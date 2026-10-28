"""Pure PyTorch versions of MTR's operators (packages/open-nnde) against direct loops.

Skipped where torch is not installed."""

import importlib
import sys
from pathlib import Path

import pytest

torch = pytest.importorskip("torch")

RUNTIME = Path(__file__).resolve().parents[2] / "packages" / "open-nnde" / "runtime"


@pytest.fixture(scope="module")
def ops():
    sys.path.insert(0, str(RUNTIME))
    try:
        return importlib.import_module("open_nnde.mtr_ops")
    finally:
        sys.path.remove(str(RUNTIME))


def _inputs(counts, k=6, nhead=2, hdim=4):
    gen = torch.Generator().manual_seed(0)
    xyz = torch.randn(sum(counts), 3, generator=gen)
    batch = torch.cat([torch.full((c,), b, dtype=torch.int32) for b, c in enumerate(counts)])
    offsets = torch.tensor([0] + list(torch.tensor(counts).cumsum(0)), dtype=torch.int32)
    feats = [torch.randn(sum(counts), nhead, hdim, generator=gen) for _ in range(3)]
    return xyz, batch, offsets, torch.tensor(counts, dtype=torch.int32), feats, k


def test_knn_returns_the_nearest_local_indices(ops):
    xyz, batch, offsets, _, _, k = _inputs([9, 3, 12])
    idx = ops.knn_batch_mlogk(xyz, xyz, batch, offsets, k)
    for p in range(len(xyz)):
        b = int(batch[p])
        start, end = int(offsets[b]), int(offsets[b + 1])
        d = ((xyz[start:end] - xyz[p]) ** 2).sum(1)
        want = set(torch.argsort(d)[: min(k, end - start)].tolist())
        got = [i for i in idx[p].tolist() if i >= 0]
        assert set(got) == want and (idx[p] < 0).sum() == max(0, k - (end - start))


def test_attention_matches_loops(ops):
    xyz, batch, offsets, counts, (q, kf, vf), k = _inputs([9, 3, 12])
    idx = ops.knn_batch_mlogk(xyz, xyz, batch, offsets, k)
    w = ops.attention_weight_computation(counts, counts, batch, idx, q, kf)
    v = ops.attention_value_computation(counts, counts, batch, idx, w, vf)
    starts = (counts.cumsum(0) - counts).tolist()
    for p in range(len(xyz)):
        start = starts[int(batch[p])]
        want_v = torch.zeros_like(v[p])
        for j, local in enumerate(idx[p].tolist()):
            if local < 0:
                assert torch.all(w[p, j] == 0)
                continue
            key = start + local
            assert torch.allclose(w[p, j], (q[p] * kf[key]).sum(-1), atol=1e-5)
            want_v += w[p, j][:, None] * vf[key]
        assert torch.allclose(v[p], want_v, atol=1e-4)
