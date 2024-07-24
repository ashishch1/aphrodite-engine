from abc import ABC, abstractmethod, abstractproperty
from typing import Dict, List, Optional, Protocol

from aphrodite.common.utils import Device


class Block(ABC):

    @abstractmethod
    def append_token_ids(self, token_ids: List[int]) -> None:
        pass

    @abstractproperty
    def block_id(self) -> Optional[int]:
        pass

    @abstractproperty
    def token_ids(self) -> List[int]:
        pass

    @abstractproperty
    def num_empty_slots(self) -> int:
        pass

    @abstractproperty
    def is_full(self) -> bool:
        pass

    @abstractproperty
    def prev_block(self) -> Optional["Block"]:
        pass

    class Factory(Protocol):

        @abstractmethod
        def __call__(
            self,
            prev_block: Optional["Block"],
            token_ids: List[int],
            block_size: int,
            allocator: "BlockAllocator",
            block_id: Optional[int] = None,
        ) -> "Block":
            pass


class BlockAllocator(ABC):

    @abstractmethod
    def allocate_mutable(self, prev_block: Optional[Block]) -> Block:
        pass

    @abstractmethod
    def allocate_immutable(self, prev_block: Optional[Block],
                           token_ids: List[int]) -> Block:
        pass

    @abstractmethod
    def free(self, block: Block) -> None:
        pass

    @abstractmethod
    def fork(self, last_block: Block) -> List[Block]:
        pass

    @abstractmethod
    def get_num_free_blocks(self) -> int:
        pass

    @abstractproperty
    def all_block_ids(self) -> frozenset[int]:
        pass

    @abstractmethod
    def clear_copy_on_writes(self) -> Dict[int, List[int]]:
        pass

    @abstractmethod
    def mark_blocks_as_accessed(self) -> None:
        pass

    @abstractmethod
    def mark_blocks_as_computed(self) -> None:
        pass

    @abstractmethod
    def get_common_computed_block_ids(
            self, seq_block_ids: List[List[int]]) -> List[int]:
        pass

    class NoFreeBlocksError(ValueError):
        pass


class DeviceAwareBlockAllocator(BlockAllocator):

    @abstractmethod
    def allocate_mutable(self, prev_block: Optional[Block],
                         device: Device) -> Block:
        pass

    @abstractmethod
    def allocate_immutable(self, prev_block: Optional[Block],
                           token_ids: List[int], device: Device) -> Block:
        pass

    @abstractmethod
    def get_num_free_blocks(self, device: Device) -> int:
        pass
