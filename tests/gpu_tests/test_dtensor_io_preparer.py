#!/usr/bin/env python3
# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the BSD-style license found in the
# LICENSE file in the root directory of this source tree.

from typing import List, Sequence, Set, Tuple

import numpy as np

import torch

import torch.distributed as dist

from torch.distributed._tensor import (
    DeviceMesh,
    distribute_tensor,
    Placement,
    Replicate,
    Shard,
)
from torch.testing._internal.common_utils import (
    instantiate_parametrized_tests,
    parametrize,
)
from torch.testing._internal.distributed._tensor.common_dtensor import (
    DTensorTestBase,
    skip_if_lt_x_gpu,
    with_comms,
)

from torchsnapshot.io_preparer import (
    DTensorIOPreparer,
    TensorBufferConsumer,
    TensorIOPreparer,
)
from torchsnapshot.manifest import NestedList

WORLD_SIZE = 4
_DEVICE_MESH = [
    list(range(WORLD_SIZE)),
    np.arange(WORLD_SIZE).reshape(2, 2).tolist(),
]
_PLACEMENTS = [
    [Shard(0)],
    [Shard(1)],
    [Shard(0), Replicate()],
    [Replicate()],
]


@instantiate_parametrized_tests
class TestDTensorIOPreparer(DTensorTestBase):
    @parametrize("shape", [(16, 32), (32, 16)])
    @parametrize("mesh", _DEVICE_MESH)
    @parametrize("placements", _PLACEMENTS)
    @skip_if_lt_x_gpu(WORLD_SIZE)
    @with_comms
    async def test_dtensor_io_preparer(
        self,
        shape: Tuple[int, int],
        mesh: NestedList,
        placements: Sequence[Placement],
    ) -> None:
        """
        Verify the basic behavior of DTensorIOPreparer prepare_write.
        """
        device_mesh = DeviceMesh("cuda", mesh=mesh)

        if len(placements) > device_mesh.ndim:
            return

        tensor = torch.rand(*shape, device="cuda")
        dtensor = distribute_tensor(tensor, device_mesh, placements)

        entry, write_reqs = DTensorIOPreparer.prepare_write(
            storage_path="/foo",
            obj=dtensor,
        )
        assert len(entry.shards) == len(write_reqs)

        # When subdivision is enabled, we have more write requests than local
        # shards, and each write request corresponds to a subview of a local
        # shard.
        assert len(dtensor._spec.num_shards) < len(write_reqs)
        entry_total_size = 0
        for shard_entry in entry.shards:
            entry_total_size += TensorIOPreparer.get_tensor_size_from_entry(
                shard_entry.tensor
            )
        assert (
            entry_total_size
            == dtensor.to_local().storage().size() * dtensor.to_local().element_size()
        )

        # Verify no overlapping locations among local shards
        locations = set()
        for shard, wr in zip(entry.shards, write_reqs):
            assert shard.tensor.location == wr.path
            locations.add(wr.path)

        assert len(locations) == len(write_reqs)

        # Verify no overlapping locations among global shards
        # pyre-ignore
        obj_list: List[Set[str]] = [None] * dist.get_world_size()
        dist.all_gather_object(obj_list, locations)
        all_locations = [location for ls in obj_list for location in ls]
        assert len(set(all_locations)) == len(all_locations)

        location_to_buf = {
            wr.path: bytes(await wr.buffer_stager.stage_buffer()) for wr in write_reqs
        }

        # Verify that the size of the storage of a persisted shard matches with the
        # shape of the shard (as opposed to the size of the storage of the shard).
        for idx, buf in enumerate(location_to_buf.values()):
            deserialized = TensorBufferConsumer.deserialize_tensor(
                buf=buf, entry=entry.shards[idx].tensor
            )
            assert (
                deserialized.storage().size() * deserialized.element_size()
                == TensorIOPreparer.get_tensor_size_from_entry(entry.shards[idx].tensor)
            )
