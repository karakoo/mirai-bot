import asyncio
import inspect
import sys
import traceback
from typing import (
    Callable,
    Dict,
    Generator,
    Hashable,
    Iterable,
    List,
    Tuple,
    Type,
    Union,
    Optional,
    NoReturn,
)

from iterwrapper import IterWrapper

from utils.context import get_var
from .entities.decorator import Decorator
from .entities.dispatcher import BaseDispatcher
from .entities.event import BaseEvent
from .entities.exectarget import ExecTarget
from .entities.inject_rule import BaseRule
from .entities.listener import Listener
from .entities.namespace import Namespace
from .entities.signatures import (
    Force,
    RemoveMe,
)
from .exceptions import (
    DisabledNamespace,
    ExecutionStop,
    ExistedNamespace,
    InvalidEventName,
    PropagationCancelled,
    RegisteredEventListener,
    RequirementCrashed,
    UnExistedNamespace,
)
from .interfaces.decorater import DecoraterInterface
from .interfaces.dispatcher import DispatcherInterface
from .typing import T_Dispatcher
from .typing import (
    T_Dispatcher,
    T_Dispatcher_Callable,
)
from .utilles import (
    argument_signature,
    dispatcher_mixin_handler,
    group_dict,
    isasyncgen,
    isgenerator,
    printer,
    run_always_await_safely,
    cached_isinstance,
)
from .zone import Zone


class Broadcast:
    loop: asyncio.AbstractEventLoop

    default_namespace: Namespace
    namespaces: List[Namespace]
    listeners: List[Listener]

    # 规则驱动注入
    dispatcher_inject_rules: List[BaseRule] = []

    dispatcher_interface: DispatcherInterface

    debug_flag: bool

    use_dispatcher_statistics: bool
    use_reference_optimization: bool

    def __init__(
            self,
            *,
            loop: asyncio.AbstractEventLoop = None,
            debug_flag: bool = None,
            inject_rules: Optional[List[BaseRule]] = None,
            use_dispatcher_statistics: bool = False,
            use_reference_optimization: bool = False
    ):
        debug: bool = get_var('debug')
        self.loop = loop or asyncio.get_event_loop()
        self.default_namespace = Namespace(name="default", default=True)
        self.debug_flag = debug if debug_flag is None else debug_flag
        self.namespaces = []
        self.listeners = []
        self.dispatcher_inject_rules = inject_rules or []
        self.dispatcher_interface = DispatcherInterface(self)

        self.use_dispatcher_statistics = use_dispatcher_statistics

        if not use_dispatcher_statistics and use_reference_optimization:
            raise ValueError(
                "the feature of reference optimization requires "
                "dispatcher statistics. "
            )
        self.use_reference_optimization = use_reference_optimization

        @self.dispatcher_interface.inject_global_raw
        def _(interface: DispatcherInterface):
            if interface.annotation is interface.event.__class__:
                return interface.event

    def default_listener_generator(self, event_class) -> Iterable[Listener]:
        return (
            IterWrapper(self.listeners)
                .filter(lambda x: not x.namespace.hide)
                .filter(lambda x: not x.namespace.disabled)
                .filter(lambda x: event_class in x.listening_events)
            # .collect(list)  # collect to a whole list
        )

    def addInjectionRule(self, rule: BaseRule) -> NoReturn:
        if rule in self.dispatcher_inject_rules:
            raise ValueError("this rule has already been added!")
        self.dispatcher_inject_rules.append(rule)

    def removeInjectionRule(self, rule: BaseRule) -> NoReturn:
        self.dispatcher_inject_rules.remove(rule)

    async def layered_scheduler(
            self,
            listener_generator: Generator[Listener, None, None],
            event: BaseEvent
    ):
        grouped: Dict[int, List[Listener]] = group_dict(
            listener_generator, lambda x: x.priority
        )
        for _, current_group in sorted(grouped.items(), key=lambda x: x[0]):
            coroutine = [
                self.Executor(target=i, event=event) for i in current_group
            ]
            try:
                await asyncio.gather(*coroutine)
            except PropagationCancelled:
                break
            except ExecutionStop:
                break
            except Exception as e:
                get_var('logger').debug(f"got an error '{type(e)}': {e}")

    async def Executor(
            self,
            target: Union[ExecTarget, Listener],
            event: BaseEvent,
            dispatchers: List[
                Union[
                    Type[BaseDispatcher],
                    Callable,
                    BaseDispatcher,
                ]
            ] = None,
            post_exception_event: bool = False,
            print_exception: bool = True,
            enableInternalAccess: bool = False,
            use_inline_generator: bool = False,
            use_dispatcher_statistics: bool = False,
            use_reference_optimization: bool = False,
    ):
        from .builtin.event import ExceptionThrew

        is_exec_target = cached_isinstance(target, ExecTarget)
        is_listener = cached_isinstance(target, Listener)

        use_dispatcher_statistics = (
                self.use_dispatcher_statistics or use_dispatcher_statistics
        )
        use_reference_optimization = (
                self.use_reference_optimization or use_reference_optimization
        )

        if is_listener:
            if target.namespace.disabled:
                raise DisabledNamespace(
                    "caught a disabled namespace: {0}"
                        .format(target.namespace.name)
                )

        if use_dispatcher_statistics and not is_exec_target:
            use_dispatcher_statistics = False
        if use_reference_optimization and not use_dispatcher_statistics:
            raise ValueError(
                "the feature of reference optimization requires dispatcher "
                "statistics."
            )

        # 先生成 dispatchers
        _dispatchers = dispatcher_mixin_handler(event.Dispatcher)
        # 开始暴力注入
        if is_exec_target:
            if target.inline_dispatchers:
                _dispatchers = target.inline_dispatchers + _dispatchers
            if is_listener:
                if target.namespace.injected_dispatchers:
                    _dispatchers = target.namespace.injected_dispatchers + \
                                   _dispatchers
        if dispatchers:
            _dispatchers = dispatchers + _dispatchers

        target_callable = target.callable if is_exec_target else target
        parameter_compile_result = {}

        async with self.dispatcher_interface.start_execution(
                event, _dispatchers, use_inline_generator
        ) as dii:
            try:
                caught_first_dispatcher = next(dii.dispatcher_pure_generator())
            except StopIteration:
                dei = DecoraterInterface(dii)  # pylint: disable=unused-variable
                self.dispatcher_interface.execution_contexts[
                    0
                ].source.dispatchers.insert(0, dei)
            else:
                if not isinstance(caught_first_dispatcher, DecoraterInterface):
                    dei = DecoraterInterface(
                        dii)  # pylint: disable=unused-variable
                    self.dispatcher_interface.execution_contexts[
                        0
                    ].source.dispatchers.insert(0, dei)
            # Decorator 的 Dispatcher 已经注入, 没他事了

            if enableInternalAccess or (
                    is_exec_target and target.enable_internal_access
            ):
                internal_access_mapping = {
                    Broadcast: self,
                    DispatcherInterface: dii
                }
                if is_exec_target:
                    internal_access_mapping[target.__class__] = target
                if is_listener:
                    internal_access_mapping[Namespace] = target.namespace

                @dii.inject_execution_raw
                def _(interface: DispatcherInterface):
                    return internal_access_mapping.get(interface.annotation)

            for injection_rule in self.dispatcher_inject_rules:
                if injection_rule.check(event, dii):
                    dii.execution_contexts[-1].dispatchers.insert(
                        1, injection_rule.target_dispatcher
                    )

            if is_exec_target:
                whole_statistics: Dict[
                    str, Dict[T_Dispatcher, Tuple[int, int]]
                ] = target.dispatcher_statistics["statistics"]
            else:
                whole_statistics = {}

            await dii.exec_lifecycle("beforeExecution")
            await dii.exec_lifecycle("beforeDispatch")
            try:
                for name, annotation, default in argument_signature(
                        target_callable):
                    statistics: Dict[
                        T_Dispatcher, Tuple[int, int]
                    ] = whole_statistics.setdefault(name, {})
                    if use_dispatcher_statistics:
                        if (
                                use_reference_optimization
                        ):  # 启用被动性质的优化特性 引用缓存(Reference Cache)
                            for dispatcher, this_statistics in sorted(
                                    statistics.items(),
                                    key=lambda x: x[1][0] / x[1][1]
                            ):
                                this_statistics[1] += 1
                                if dispatcher() is None:
                                    # ref 在对象 dead 时返回 None.
                                    del statistics[dispatcher]
                                    continue

                                try:
                                    result = await dii.lookup_by_directly(
                                        dispatcher(), name, annotation,
                                        default
                                    )
                                except RequirementCrashed:
                                    # 忽略单个 Dispatcher 执行时若无法满足要求时所引发的错误
                                    pass
                                except:
                                    traceback.print_exc()
                                    raise
                                else:
                                    this_statistics[0] += 1
                                    parameter_compile_result[name] = result
                            else:
                                if parameter_compile_result.get(name):
                                    continue

                        (
                            parameter_compile_result[name],
                            target_dispatcher,
                            target_source,
                            past_dispatchers,
                        ) = await dii.lookup_param(
                            name, annotation, default,
                            enable_extra_return=True
                        )

                        # TODO: 判定 target_source 来源的可靠性(毕竟...)
                        if target_source.source() is not None:
                            # 1.成功给出结果的次数, 2.总共被 call 的次数
                            statistics.setdefault(target_dispatcher, [0, 0])
                            this_statistics = statistics[target_dispatcher]
                            this_statistics[0] += 1
                            this_statistics[1] += 1

                            if len(statistics) > 1:
                                past_set = set(statistics.keys())
                                for past_dispatcher in past_dispatchers:
                                    if past_dispatcher in past_set:
                                        statistics[past_dispatcher][1] += 1
                    else:
                        parameter_compile_result[name] = await dii.lookup_param(
                            name, annotation, default
                        )
                if is_exec_target:
                    if target.headless_decorators:
                        for hl_d in target.headless_decorators:
                            await dii.lookup_param(None, None, hl_d)
            except ExecutionStop:
                raise
            except RequirementCrashed:
                traceback.print_exc()
                raise
            except Exception as e:
                if print_exception:
                    traceback.print_exc()
                if post_exception_event:
                    self.postEvent(ExceptionThrew(exception=e, event=event))
                raise
            finally:
                await dii.exec_lifecycle("afterDispatch")

            await dii.exec_lifecycle("beforeTargetExec")

            try:
                result = await run_always_await_safely(
                    target_callable, **parameter_compile_result
                )
            except ExecutionStop:
                raise  # 直接抛出.
            except PropagationCancelled:
                raise  # 防止事件广播
            except Exception as e:
                if print_exception:
                    traceback.print_exc()
                if post_exception_event:
                    # 如果没有referrer, 则广播事件, 如果有则向上抛出(防止重复抛出事件)
                    self.postEvent(ExceptionThrew(exception=e, event=event))
                raise
            finally:
                _, exception, tb = sys.exc_info()
                await dii.exec_lifecycle("afterTargetExec", exception, tb)
                await dii.exec_lifecycle("afterExecution", exception, tb)

            if [inspect.isgenerator, isgenerator][isinstance(result, Hashable)](
                    result):
                generate = result
                try:
                    result = next(generate)
                except StopIteration as e:
                    result = e.value
                else:
                    dii.alive_generator_dispatcher[-1].append((generate, False))
            elif [inspect.isasyncgen, isasyncgen][isinstance(result, Hashable)](
                    result):
                generate = result
                try:
                    result = await generate.__anext__()
                except StopAsyncIteration:
                    return  # value = None
                else:
                    dii.alive_generator_dispatcher[-1].append((generate, True))

            if result.__class__ is Force:
                return result.content

            if result.__class__ is RemoveMe:
                if cached_isinstance(target, Listener):
                    if target in self.listeners:
                        self.listeners.pop(self.listeners.index(target))
            return result

    def postEvent(self, event: BaseEvent):
        self.loop.create_task(
            self.layered_scheduler(
                listener_generator=self.default_listener_generator(
                    event.__class__),
                event=event,
            )
        )

    @staticmethod
    def event_class_generator(target=BaseEvent):
        for i in target.__subclasses__():
            yield i
            if i.__subclasses__():
                yield from Broadcast.event_class_generator(i)

    @staticmethod
    def findEvent(name: str):
        for i in Broadcast.event_class_generator():
            if i.__name__ == name:
                return i

    @property
    def getDefaultNamespace(self):
        return self.default_namespace

    def createNamespace(
            self, name, *, priority: int = 0, hide: bool = False,
            disabled: bool = False
    ):
        result = self.getNamespace(name)
        if result is not None:
            return result
        result = Namespace(name=name, priority=priority, hide=hide,
                           disabled=disabled)
        self.namespaces.append(result)
        return result

    def removeNamespace(self, name):
        if self.containNamespace(name):
            for namespace in self.namespaces:
                if namespace.name == name:
                    self.namespaces.remove(namespace)
            for listener in self.listeners:
                if listener.namespace.name == name:
                    self.listeners.remove(listener)
        else:
            raise UnExistedNamespace(name)

    def containNamespace(self, name):
        for i in self.namespaces:
            if i.name == name:
                return True
        return False

    def getNamespace(self, name):
        if self.containNamespace(name):
            for i in self.namespaces:
                if i.name == name:
                    return i
        else:
            return None

    def hideNamespace(self, name):
        ns = self.getNamespace(name)
        ns.hide = True

    def unHideNamespace(self, name):
        ns = self.getNamespace(name)
        ns.hide = False

    def disableNamespace(self, name):
        ns = self.getNamespace(name)
        ns.disabled = True

    def enableNamespace(self, name):
        ns = self.getNamespace(name)
        ns.disabled = False

    def containListener(self, target):
        for i in self.listeners:
            if i.callable == target:
                return True
        return False

    def getListener(self, target):
        for i in self.listeners:
            if i.callable == target:
                return i

    def removeListener(self, target):
        if self.containListener(target):
            self.listeners.remove(target)

    def addListener(self, target):
        if not self.containListener(target):
            self.listeners.append(target)

    def includeZone(self, target_zone: Zone):
        if self.containNamespace(Zone.namespace.name):
            raise ValueError(
                "the zone cannot import because its name has been used."
            )
        self.namespaces.append(target_zone.namespace)
        self.listeners.extend(target_zone.listeners)

    def receiver(
            self,
            event: Union[str, Type[BaseEvent]],
            priority: int = 16,
            dispatchers=None,
            namespace: Namespace = None,
            headless_decorators=None,
            enable_internal_access: bool = False,
    ):
        if dispatchers is None:
            dispatchers = []
        if headless_decorators is None:
            headless_decorators = []
        if cached_isinstance(event, str):
            _name = event
            event = self.findEvent(event)
            if not event:
                raise InvalidEventName(_name, "is not valid!")
        priority = (type(priority) == int) and priority or int(priority)  # 类型转换

        def receiver_wrapper(callable_target):
            may_listener = self.getListener(callable_target)
            if not may_listener:
                self.listeners.append(
                    Listener(
                        callable_=callable_target,
                        namespace=namespace or self.getDefaultNamespace,
                        inline_dispatchers=dispatchers,
                        priority=priority,
                        listening_events=[event],
                        headless_decorators=headless_decorators,
                        enable_internal_access=enable_internal_access,
                    )
                )
            else:
                if event not in may_listener.listening_events:
                    may_listener.listening_events.append(event)
                else:
                    raise RegisteredEventListener(
                        event.__name__, "has been registered!"
                    )
            return callable_target

        return receiver_wrapper
