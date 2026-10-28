"""Map artifacts of a MapStore package for the open Neural NDE runtime.

TeraSim stores ``cnde_map.pkl`` and ``cnde_id_mapping.pkl`` next to ``net.net.xml``. The open
runtime reads the network itself, so the map file only records how it was made; the id mapping
sends every SUMO lane to itself (it carries traffic light states, which MTR does not read).
"""

import pickle


def create_map_data_from_netxml(net_xml, map_pkl, id_mapping_pkl):
    from open_nnde.sumo_map import map_from_net

    mtr_map = map_from_net(net_xml)
    with open(map_pkl, "wb") as f:
        pickle.dump(
            {
                "format": "open-nnde/1",
                "net_xml": str(net_xml),
                "pos_offset": mtr_map.pos_offset.tolist(),
                "polylines": len(mtr_map.polylines),
                "points": mtr_map.num_points,
            },
            f,
        )
    with open(id_mapping_pkl, "wb") as f:
        pickle.dump({lane_id: [lane_id] for lane_id in mtr_map.lane_ids}, f)
