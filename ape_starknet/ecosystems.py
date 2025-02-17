from enum import Enum
from itertools import zip_longest
from typing import Any, Dict, Iterator, List, Optional, Tuple, Type, Union

from ape.api import BlockAPI, EcosystemAPI, ReceiptAPI, TransactionAPI
from ape.api.networks import ProxyInfoAPI
from ape.contracts import ContractContainer, ContractInstance
from ape.types import AddressType, ContractLog, RawAddress
from eth_utils import is_0x_prefixed
from ethpm_types import ContractType
from ethpm_types.abi import ConstructorABI, EventABI, EventABIType, MethodABI
from hexbytes import HexBytes
from starknet_py.constants import OZ_PROXY_STORAGE_KEY
from starknet_py.net.models.address import parse_address
from starknet_py.net.models.chains import StarknetChainId
from starknet_py.utils.data_transformer import DataTransformer
from starkware.starknet.definitions.fields import ContractAddressSalt
from starkware.starknet.definitions.transaction_type import TransactionType
from starkware.starknet.public.abi import get_selector_from_name
from starkware.starknet.public.abi_structs import identifier_manager_from_abi
from starkware.starknet.services.api.contract_class import ContractClass

from ape_starknet.exceptions import StarknetEcosystemError
from ape_starknet.transactions import (
    ContractDeclaration,
    DeclareTransaction,
    DeployReceipt,
    DeployTransaction,
    InvocationReceipt,
    InvokeFunctionTransaction,
    StarknetTransaction,
)
from ape_starknet.utils import to_checksum_address
from ape_starknet.utils.basemodel import StarknetBase

NETWORKS = {
    # chain_id, network_id
    "mainnet": (StarknetChainId.MAINNET.value, StarknetChainId.MAINNET.value),
    "testnet": (StarknetChainId.TESTNET.value, StarknetChainId.TESTNET.value),
}


class StarknetBlock(BlockAPI):
    """
    A block in Starknet.
    """


class ProxyType(Enum):
    LEGACY = 0
    ARGENT_X = 1
    OPEN_ZEPPELIN = 2


class StarknetProxy(ProxyInfoAPI):
    """
    A proxy contract in Starknet.
    """

    type: ProxyType


class Starknet(EcosystemAPI, StarknetBase):
    """
    The Starknet ``EcosystemAPI`` implementation.
    """

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__}>"

    @classmethod
    def decode_address(cls, raw_address: RawAddress) -> AddressType:
        """
        Make a checksum address given a supported format.
        Borrowed from ``eth_utils.to_checksum_address()`` but supports
        non-length 42 addresses.
        Args:
            raw_address (Union[int, str, bytes]): The value to convert.
        Returns:
            ``AddressType``: The converted address.
        """
        return to_checksum_address(raw_address)

    @classmethod
    def encode_address(cls, address: Union[AddressType, str]) -> int:
        return parse_address(address)

    def serialize_transaction(self, transaction: TransactionAPI) -> bytes:
        if not isinstance(transaction, StarknetTransaction):
            raise StarknetEcosystemError(f"Can only serialize '{StarknetTransaction.__name__}'.")

        starknet_object = transaction.as_starknet_object()
        return starknet_object.deserialize()

    def decode_returndata(self, abi: MethodABI, raw_data: List[int]) -> Any:  # type: ignore
        if not raw_data:
            return raw_data

        raw_data = [self.encode_primitive_value(v) for v in raw_data]
        iter_data = iter(raw_data)
        decoded: List[Any] = []

        # Given that the caller is StarkNetProvider.send_transaction().
        # In the caller, we removed the first item which was the total items when
        # a sender is specified to the invoke TX.
        # Now, we are dealing with a 1-item array, it's safe to simply return it.
        if len(raw_data) == 1:
            return raw_data[0]

        for abi_output_cur, abi_output_next in zip_longest(abi.outputs, abi.outputs[1:]):
            if abi_output_cur.type == "Uint256":
                # Unint256 are stored using 2 slots
                decoded.append((next(iter_data), next(iter_data)))
            elif (
                abi_output_cur.type == "felt"
                and abi_output_next
                and abi_output_next.type == "felt*"
            ):
                # Array - strip off leading length
                array_len = next(iter_data)
                decoded.append([next(iter_data) for _ in range(array_len)])  # type: ignore
            elif abi_output_cur.type == "felt*":
                # The array was handled by the previous condition at the previous iteration
                continue
            else:
                decoded.append(next(iter_data))

        # Keep only the expected data instead of a 1-item array
        if len(abi.outputs) == 1 or (len(abi.outputs) == 2 and abi.outputs[1].type == "felt*"):
            decoded = decoded[0]

        return decoded

    def encode_calldata(
        self,
        full_abi: List,
        method_abi: Union[ConstructorABI, MethodABI],
        call_args: Union[List, Tuple],
    ) -> List:
        full_abi = [abi.dict() if hasattr(abi, "dict") else abi for abi in full_abi]
        id_manager = identifier_manager_from_abi(full_abi)
        transformer = DataTransformer(method_abi.dict(), id_manager)
        pre_encoded_args: List[Any] = []
        index = 0
        last_index = min(len(method_abi.inputs), len(call_args)) - 1
        did_process_array_during_arr_len = False

        for call_arg, input_type in zip(call_args, method_abi.inputs):
            if str(input_type.type).endswith("*"):
                if did_process_array_during_arr_len:
                    did_process_array_during_arr_len = False
                    continue

                encoded_arg = self._pre_encode_value(call_arg)
                pre_encoded_args.append(encoded_arg)
            elif (
                input_type.name is not None
                and input_type.name.endswith("_len")
                and index < last_index
                and str(method_abi.inputs[index + 1].type).endswith("*")
            ):
                pre_encoded_arg = self._pre_encode_value(call_arg)

                if isinstance(pre_encoded_arg, int):
                    # 'arr_len' was provided.
                    array_index = index + 1
                    pre_encoded_array = self._pre_encode_array(call_args[array_index])
                    pre_encoded_args.append(pre_encoded_array)
                    did_process_array_during_arr_len = True
                else:
                    pre_encoded_args.append(pre_encoded_arg)

            else:
                pre_encoded_args.append(self._pre_encode_value(call_arg))

            index += 1

        encoded_calldata, _ = transformer.from_python(*pre_encoded_args)
        return encoded_calldata

    def _pre_encode_value(self, value: Any) -> Any:
        if isinstance(value, dict):
            return self._pre_encode_struct(value)
        elif isinstance(value, (list, tuple)):
            return self._pre_encode_array(value)
        else:
            return self.encode_primitive_value(value)

    def _pre_encode_array(self, array: Any) -> List:
        if not isinstance(array, (list, tuple)):
            # Will handle single item structs and felts.
            return self._pre_encode_array([array])

        encoded_array = []
        for item in array:
            encoded_value = self._pre_encode_value(item)
            encoded_array.append(encoded_value)

        return encoded_array

    def _pre_encode_struct(self, struct: Dict) -> Dict:
        encoded_struct = {}
        for key, value in struct.items():
            encoded_struct[key] = self._pre_encode_value(value)

        return encoded_struct

    def encode_primitive_value(self, value: Any) -> int:
        if isinstance(value, int):
            return value

        elif isinstance(value, str) and is_0x_prefixed(value):
            return int(value, 16)

        elif isinstance(value, HexBytes):
            return int(value.hex(), 16)

        return value

    def decode_receipt(self, data: dict) -> ReceiptAPI:
        txn_type = TransactionType(data["type"])
        receipt_cls: Union[Type[ContractDeclaration], Type[DeployReceipt], Type[InvocationReceipt]]
        if txn_type == TransactionType.INVOKE_FUNCTION:
            receipt_cls = InvocationReceipt
        elif txn_type == TransactionType.DEPLOY:
            receipt_cls = DeployReceipt
        elif txn_type == TransactionType.DECLARE:
            receipt_cls = ContractDeclaration
        else:
            raise ValueError(f"Unable to handle contract type '{txn_type.value}'.")

        receipt = receipt_cls.parse_obj(data)

        if receipt is None:
            raise ValueError("Failed to parse receipt from data.")

        return receipt

    def decode_block(self, data: dict) -> BlockAPI:
        return StarknetBlock(
            hash=HexBytes(data["block_hash"]),
            number=data["block_number"],
            parentHash=HexBytes(data["parent_block_hash"]),
            size=len(data["transactions"]),  # TODO: Figure out size
            timestamp=data["timestamp"],
        )

    def encode_deployment(
        self, deployment_bytecode: HexBytes, abi: ConstructorABI, *args, **kwargs
    ) -> TransactionAPI:
        salt = kwargs.get("salt")
        if not salt:
            salt = ContractAddressSalt.get_random_value()

        constructor_args = list(args)
        contract = ContractClass.deserialize(deployment_bytecode)
        calldata = self.encode_calldata(contract.abi, abi, constructor_args)
        return DeployTransaction(
            salt=salt,
            constructor_calldata=calldata,
            contract_code=contract.dumps(),
            token=kwargs.get("token"),
        )

    def encode_transaction(
        self, address: AddressType, abi: MethodABI, *args, **kwargs
    ) -> TransactionAPI:
        # NOTE: This method only works for invoke-transactions
        contract_type = self.chain_manager.contracts[address]
        encoded_calldata = self.encode_calldata(contract_type.abi, abi, list(args))

        return InvokeFunctionTransaction(
            contract_address=address,
            method_abi=abi,
            calldata=encoded_calldata,
            sender=kwargs.get("sender"),
            max_fee=kwargs.get("max_fee", 0),
        )

    def encode_contract_declaration(
        self, contract: Union[ContractContainer, ContractType], *args, **kwargs
    ) -> DeclareTransaction:
        contract_type = (
            contract.contract_type if isinstance(contract, ContractContainer) else contract
        )
        code = (
            (contract_type.deployment_bytecode.bytecode or 0)
            if contract_type.deployment_bytecode
            else 0
        )
        starknet_contract = ContractClass.deserialize(HexBytes(code))
        return DeclareTransaction(contract_type=contract_type, data=starknet_contract.dumps())

    def create_transaction(self, **kwargs) -> TransactionAPI:
        txn_type = TransactionType(kwargs.pop("type", kwargs.pop("tx_type", "")))
        txn_cls: Union[
            Type[InvokeFunctionTransaction], Type[DeployTransaction], Type[DeclareTransaction]
        ]
        invoking = txn_type == TransactionType.INVOKE_FUNCTION
        if invoking:
            txn_cls = InvokeFunctionTransaction
        elif txn_type == TransactionType.DEPLOY:
            txn_cls = DeployTransaction
        elif txn_type == TransactionType.DECLARE:
            txn_cls = DeclareTransaction

        txn_data: Dict[str, Any] = {**kwargs, "signature": None}
        if "chain_id" not in txn_data and self.network_manager.active_provider:
            txn_data["chain_id"] = self.provider.chain_id

        # For deploy-txns, 'contract_address' is the address of the newly deployed contract.
        if "contract_address" in txn_data:
            txn_data["contract_address"] = self.decode_address(txn_data["contract_address"])

        if not invoking:
            return txn_cls(**txn_data)

        """ ~ Invoke transactions ~ """

        if "method_abi" not in txn_data:
            contract_int = txn_data["contract_address"]
            contract_str = self.decode_address(contract_int)
            contract = self.chain_manager.contracts.get(contract_str)
            if not contract:
                raise ValueError("Unable to create transaction objects from other networks.")

            selector = txn_data["entry_point_selector"]
            if isinstance(selector, str):
                selector = int(selector, 16)

            for abi in contract.mutable_methods:
                selector_to_check = get_selector_from_name(abi.name)

                if selector == selector_to_check:
                    txn_data["method_abi"] = abi

        if "calldata" in txn_data and txn_data["calldata"] is not None:
            # Transactions in blocks show calldata as flattened hex-strs
            # but elsewhere we expect flattened ints. Convert to ints for
            # consistency and testing purposes.
            encoded_calldata = [self.encode_primitive_value(v) for v in txn_data["calldata"]]
            txn_data["calldata"] = encoded_calldata

        return txn_cls(**txn_data)

    def decode_logs(self, abi: EventABI, raw_logs: List[Dict]) -> Iterator[ContractLog]:
        event_key = get_selector_from_name(abi.name)
        matching_logs = [log for log in raw_logs if event_key in log["keys"]]

        def decode_items(
            abi_types: List[EventABIType], data: List[int]
        ) -> List[Union[int, Tuple[int, int]]]:
            decoded: List[Union[int, Tuple[int, int]]] = []
            iter_data = iter(data)
            for abi_type in abi_types:
                if abi_type.type == "Uint256":
                    # unint256 are stored using 2 slots
                    decoded.append((next(iter_data), next(iter_data)))
                else:
                    decoded.append(next(iter_data))
            return decoded

        for index, log in enumerate(matching_logs):
            event_args = dict(
                zip([a.name for a in abi.inputs], decode_items(abi.inputs, log["data"]))
            )
            yield ContractLog(  # type: ignore
                name=abi.name,
                index=index,
                event_arguments=event_args,
                transaction_hash=log["transaction_hash"],
                block_hash=log["block_hash"],
                block_number=log["block_number"],
            )

    def get_proxy_info(self, address: AddressType) -> Optional[StarknetProxy]:
        contract = self.chain_manager.contracts.instance_at(address)
        if not isinstance(contract, ContractInstance):
            return None

        proxy_type: Optional[ProxyType] = None
        target: Optional[int] = None

        # Legacy proxy check
        if "implementation" in contract.contract_type.view_methods:
            instance = self.chain_manager.contracts.instance_at(address)
            target = instance.implementation()  # type: ignore
            proxy_type = ProxyType.LEGACY

        # Argent-X proxy check
        elif "get_implementation" in contract.contract_type.view_methods:
            instance = self.chain_manager.contracts.instance_at(address)
            target = instance.get_implementation()  # type: ignore
            proxy_type = ProxyType.ARGENT_X

        # OpenZeppelin proxy check
        elif self.provider.client is not None:
            address_int = self.encode_address(address)
            target = self.provider.client.get_storage_at_sync(
                contract_address=address_int, key=OZ_PROXY_STORAGE_KEY
            )
            if target == "0x0":
                target = None
            else:
                proxy_type = ProxyType.OPEN_ZEPPELIN

        return (
            StarknetProxy(target=self.decode_address(target), type=proxy_type)
            if target and proxy_type
            else None
        )
