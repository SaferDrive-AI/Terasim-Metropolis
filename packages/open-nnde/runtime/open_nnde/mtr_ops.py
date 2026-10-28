"""Pure PyTorch versions of MTR's two compiled ops, so MTR runs without a CUDA toolchain.

Same semantics as MTR's CUDA kernels (mtr/ops/knn/src/knn_gpu.cu,
mtr/ops/attention/src/attention_*_kernel_v2.cu), for inference:

* knn_batch_mlogk: for every point, the k nearest query points of its batch, as indices
  local to that batch; -1 where the batch has fewer than k query points. The kernel returns
  them in heap order, this version by distance; the local attention that consumes them is
  invariant to the order.
* attention_weight_computation / attention_value_computation: local attention over the key
  indices of ``index_pair`` (local to the query's batch, -1 = no key), weight = query . key,
  value = sum of weight * value over the valid keys.
"""

from __future__ import annotations

import sys
import types

import torch

QUERY_CHUNK = 16384


def knn_batch_mlogk(xyz, query_xyz, batch_idxs, query_batch_offsets, k):
    n = xyz.shape[0]
    idx = torch.full((n, k), -1, dtype=torch.int32, device=xyz.device)
    offsets = [int(o) for o in query_batch_offsets.tolist()]
    batch_idxs = batch_idxs.long()
    for b in range(len(offsets) - 1):
        start, end = offsets[b], offsets[b + 1]
        if end <= start:
            continue
        points = torch.nonzero(batch_idxs == b, as_tuple=True)[0]
        if points.numel() == 0:
            continue
        kk = min(k, end - start)
        dist = torch.cdist(xyz[points].float(), query_xyz[start:end].float())
        idx[points, :kk] = dist.topk(kk, dim=1, largest=False).indices.int()
    return idx


def _key_index(key_batch_cnt, index_pair_batch, index_pair):
    counts = key_batch_cnt.long()
    key_start = torch.cumsum(counts, 0) - counts
    start = key_start[index_pair_batch.long()]
    valid = index_pair >= 0
    index = torch.where(
        valid, index_pair.long() + start[:, None], torch.zeros_like(index_pair, dtype=torch.long)
    )
    return index, valid


def attention_weight_computation(
    query_batch_cnt, key_batch_cnt, index_pair_batch, index_pair, query_features, key_features
):
    index, valid = _key_index(key_batch_cnt, index_pair_batch, index_pair)
    total, local = index_pair.shape
    out = query_features.new_zeros(total, local, query_features.shape[1])
    for s in range(0, total, QUERY_CHUNK):
        e = s + QUERY_CHUNK
        keys = key_features[index[s:e]]  # (q, L, H, D)
        out[s:e] = torch.einsum("qhd,qlhd->qlh", query_features[s:e], keys) * valid[s:e, :, None]
    return out


def attention_value_computation(
    query_batch_cnt, key_batch_cnt, index_pair_batch, index_pair, attn_weight, value_features
):
    index, valid = _key_index(key_batch_cnt, index_pair_batch, index_pair)
    total = index_pair.shape[0]
    out = value_features.new_zeros(total, value_features.shape[1], value_features.shape[2])
    for s in range(0, total, QUERY_CHUNK):
        e = s + QUERY_CHUNK
        values = value_features[index[s:e]]  # (q, L, H, D)
        weight = attn_weight[s:e] * valid[s:e, :, None]
        out[s:e] = torch.einsum("qlh,qlhd->qhd", weight, values)
    return out


def install():
    """Make ``import mtr.ops...`` work without the compiled extensions, then route MTR's op
    functions to the versions above. Call before anything imports MTR's models."""
    for name in ("mtr.ops.attention.attention_cuda", "mtr.ops.knn.knn_cuda"):
        sys.modules.setdefault(name, types.ModuleType(name))
    from mtr.ops.attention import attention_utils, attention_utils_v2
    from mtr.ops.knn import knn_utils

    for module in (attention_utils, attention_utils_v2):
        module.attention_weight_computation = attention_weight_computation
        module.attention_value_computation = attention_value_computation
    knn_utils.knn_batch_mlogk = knn_batch_mlogk
