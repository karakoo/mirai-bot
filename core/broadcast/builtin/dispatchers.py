from typing import List

from ..abstract.interfaces.dispatcher import IDispatcherInterface
from ..entities.dispatcher import BaseDispatcher
from ..entities.mapping_rule import MappingRule
from ..entities.signatures import Force


def SimpleMapping(rules: List[MappingRule]):
    class mapping_dispatcher(BaseDispatcher):
        @staticmethod
        def catch(interface: IDispatcherInterface):
            for rule in rules:
                if rule.mode(interface):
                    return Force(rule.value)

    return mapping_dispatcher


def Hook(condition, fixer):
    class hook_dispatcher(BaseDispatcher):
        @staticmethod
        def catch(interface: IDispatcherInterface):
            if condition(interface):
                return fixer(
                        interface.lookup_param(
                                interface.name, interface.annotation,
                                interface.default
                        )
                )

    return hook_dispatcher
