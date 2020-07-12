# Copyright (c) 2020 Tulir Asokan
#
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.
from typing import Dict, List, Callable, Union, Optional, Awaitable, Any, Type, Tuple, TYPE_CHECKING
from abc import ABC, abstractmethod
from enum import Enum, Flag, auto
from time import time
import asyncio

from mautrix.errors import MUnknownToken
from mautrix.types import (EventType, MessageEvent, StateEvent, StrippedStateEvent, Event, FilterID,
                           Filter, AccountDataEvent, DeviceLists, DeviceOTKCount, EphemeralEvent,
                           PresenceState, ToDeviceEvent, SyncToken, UserID, JSON)
from mautrix.util.logging import TraceLogger

from .state_store import SyncStore, MemorySyncStore

if TYPE_CHECKING:
    from .dispatcher import Dispatcher
    from .client import Client

EventHandler = Callable[[Event], Awaitable[None]]


class SyncStream(Flag):
    INTERNAL = auto()

    JOINED_ROOM = auto()
    INVITED_ROOM = auto()
    LEFT_ROOM = auto()

    TIMELINE = auto()
    STATE = auto()
    EPHEMERAL = auto()
    ACCOUNT_DATA = auto()
    TO_DEVICE = auto()


class InternalEventType(Enum):
    SYNC_STARTED = auto()
    SYNC_ERRORED = auto()
    SYNC_SUCCESSFUL = auto()
    SYNC_STOPPED = auto()

    JOIN = auto()
    PROFILE_CHANGE = auto()
    INVITE = auto()
    REJECT_INVITE = auto()
    DISINVITE = auto()
    LEAVE = auto()
    KICK = auto()
    BAN = auto()
    UNBAN = auto()

    DEVICE_LISTS = auto()
    DEVICE_OTK_COUNT = auto()


class Syncer(ABC):
    loop: asyncio.AbstractEventLoop
    log: TraceLogger
    mxid: UserID

    global_event_handlers: List[Tuple[EventHandler, bool]]
    event_handlers: Dict[Union[EventType, InternalEventType], List[Tuple[EventHandler, bool]]]
    dispatchers: Dict[Type['Dispatcher'], 'Dispatcher']
    syncing_task: Optional[asyncio.Future]
    ignore_initial_sync: bool
    ignore_first_sync: bool
    presence: PresenceState

    sync_store: SyncStore

    def __init__(self, sync_store: SyncStore) -> None:
        self.global_event_handlers = []
        self.event_handlers = {}
        self.dispatchers = {}
        self.syncing_task = None
        self.ignore_initial_sync = False
        self.ignore_first_sync = False
        self.presence = PresenceState.ONLINE

        self.sync_store = sync_store or MemorySyncStore()

    def on(self, var: Union[EventHandler, EventType, InternalEventType]
           ) -> Union[EventHandler, Callable[[EventHandler], EventHandler]]:
        """
        Add a new event handler. This method is for decorator usage.
        Use :meth:`add_event_handler` if you don't use a decorator.

        Args:
            var: Either the handler function or the event type to handle.

        Returns:
            If ``var`` was the handler function, the handler function is returned.

            If ``var`` was an event type, a function that takes the handler function as an argument
            is returned.

        Examples:
            >>> client = Client(...)
            >>> @client.on(EventType.ROOM_MESSAGE)
            >>> def handler(event: MessageEvent) -> None:
            ...     pass
        """
        if isinstance(var, (EventType, InternalEventType)):
            def decorator(func: EventHandler) -> EventHandler:
                self.add_event_handler(var, func)
                return func

            return decorator
        else:
            self.add_event_handler(EventType.ALL, var)
            return var

    def add_dispatcher(self, dispatcher_type: Type['Dispatcher']) -> None:
        if dispatcher_type in self.dispatchers:
            return
        self.log.debug(f"Enabling {dispatcher_type.__name__}")
        self.dispatchers[dispatcher_type] = dispatcher_type(self)
        self.dispatchers[dispatcher_type].register()

    def remove_dispatcher(self, dispatcher_type: Type['Dispatcher']) -> None:
        if dispatcher_type not in self.dispatchers:
            return
        self.log.debug(f"Disabling {dispatcher_type.__name__}")
        self.dispatchers[dispatcher_type].unregister()
        del self.dispatchers[dispatcher_type]

    def add_event_handler(self, event_type: Union[InternalEventType, EventType],
                          handler: EventHandler, wait_sync: bool = False) -> None:
        """
        Add a new event handler.

        Args:
            event_type: The event type to add. If not specified, the handler will be called for all
                event types.
            handler: The handler function to add.
            wait_sync: Whether or not the handler should be awaited before the next sync request.
        """
        if not isinstance(event_type, (EventType, InternalEventType)):
            raise ValueError("Invalid event type")
        if event_type == EventType.ALL:
            self.global_event_handlers.append((handler, wait_sync))
        else:
            self.event_handlers.setdefault(event_type, []).append((handler, wait_sync))

    def remove_event_handler(self, event_type: Union[EventType, InternalEventType],
                             handler: EventHandler) -> None:
        """
        Remove an event handler.

        Args:
            handler: The handler function to remove.
            event_type: The event type to remove the handler function from.
        """
        if not isinstance(event_type, (EventType, InternalEventType)):
            raise ValueError("Invalid event type")
        try:
            if event_type == EventType.ALL:
                # FIXME this is a bit hacky
                self.global_event_handlers.remove((handler, True))
                self.global_event_handlers.remove((handler, False))
            else:
                handlers = self.event_handlers[event_type]
                handlers.remove((handler, True))
                handlers.remove((handler, False))
                if len(handlers) == 0:
                    del self.event_handlers[event_type]
        except (KeyError, ValueError):
            pass

    def dispatch_event(self, event: Event, source: SyncStream) -> List[asyncio.Task]:
        """
        Send the given event to all applicable event handlers.

        Args:
            event: The event to send.
            source: The sync stream the event was received in.
        """
        if isinstance(event, MessageEvent):
            event.content.trim_reply_fallback()
        if getattr(event, "state_key", None) is not None:
            event.type = event.type.with_class(EventType.Class.STATE)
        elif source & SyncStream.EPHEMERAL:
            event.type = event.type.with_class(EventType.Class.EPHEMERAL)
        elif source & SyncStream.ACCOUNT_DATA:
            event.type = event.type.with_class(EventType.Class.ACCOUNT_DATA)
        elif source & SyncStream.TO_DEVICE:
            event.type = event.type.with_class(EventType.Class.TO_DEVICE)
        else:
            event.type = event.type.with_class(EventType.Class.MESSAGE)
        setattr(event, "source", source)
        return self.dispatch_manual_event(event.type, event, include_global_handlers=True)

    async def _catch_errors(self, handler: EventHandler, data: Any) -> None:
        try:
            await handler(data)
        except Exception:
            self.log.exception("Failed to run handler")

    def dispatch_manual_event(self, event_type: Union[EventType, InternalEventType],
                              data: Any, include_global_handlers: bool = False,
                              force_synchronous: bool = False) -> List[asyncio.Task]:
        handlers = self.event_handlers.get(event_type, [])
        if include_global_handlers:
            handlers = self.global_event_handlers + handlers
        tasks = []
        for handler, wait_sync in handlers:
            task = self.loop.create_task(self._catch_errors(handler, data))
            if force_synchronous or wait_sync:
                tasks.append(task)
        return tasks

    async def run_internal_event(self, event_type: InternalEventType, custom_type: Any = None,
                                 **kwargs: Any) -> None:
        kwargs["source"] = SyncStream.INTERNAL
        tasks = self.dispatch_manual_event(event_type, custom_type or kwargs,
                                           include_global_handlers=False)
        await asyncio.gather(*tasks)

    def dispatch_internal_event(self, event_type: InternalEventType, custom_type: Any = None,
                                **kwargs: Any) -> List[asyncio.Task]:
        kwargs["source"] = SyncStream.INTERNAL
        return self.dispatch_manual_event(event_type, custom_type or kwargs,
                                          include_global_handlers=False)

    def handle_sync(self, data: JSON) -> List[asyncio.Task]:
        """
        Handle a /sync object.

        Args:
            data: The data from a /sync request.
        """
        tasks = []

        otk_count = data.get("device_one_time_keys_count", {})
        tasks += self.dispatch_internal_event(
            InternalEventType.DEVICE_OTK_COUNT,
            custom_type=DeviceOTKCount(curve25519=otk_count.get("curve25519", 0),
                                       signed_curve25519=otk_count.get("signed_curve25519", 0)))

        device_lists = data.get("device_lists", {})
        tasks += self.dispatch_internal_event(InternalEventType.DEVICE_LISTS,
                                              custom_type=DeviceLists(
                                                  changed=device_lists.get("changed", []),
                                                  left=device_lists.get("left", [])))

        for raw_event in data.get("account_data", {}).get("events", []):
            tasks += self.dispatch_event(AccountDataEvent.deserialize(raw_event),
                                         source=SyncStream.ACCOUNT_DATA)
        for raw_event in data.get("ephemeral", {}).get("events", []):
            tasks += self.dispatch_event(EphemeralEvent.deserialize(raw_event),
                                         source=SyncStream.EPHEMERAL)
        for raw_event in data.get("to_device", {}).get("events", []):
            tasks += self.dispatch_event(ToDeviceEvent.deserialize(raw_event),
                                         source=SyncStream.TO_DEVICE)

        rooms = data.get("rooms", {})
        for room_id, room_data in rooms.get("join", {}).items():
            for raw_event in room_data.get("state", {}).get("events", []):
                raw_event["room_id"] = room_id
                tasks += self.dispatch_event(StateEvent.deserialize(raw_event),
                                             source=SyncStream.JOINED_ROOM | SyncStream.STATE)

            for raw_event in room_data.get("timeline", {}).get("events", []):
                raw_event["room_id"] = room_id
                tasks += self.dispatch_event(Event.deserialize(raw_event),
                                             source=SyncStream.JOINED_ROOM | SyncStream.TIMELINE)
        for room_id, room_data in rooms.get("invite", {}).items():
            events: List[Dict[str, Any]] = room_data.get("invite_state", {}).get("events", [])
            for raw_event in events:
                raw_event["room_id"] = room_id
            raw_invite = next(raw_event for raw_event in events
                              if raw_event.get("type", "") == "m.room.member"
                              and raw_event.get("state_key", "") == self.mxid)
            # These aren't required by the spec, so make sure they're set
            raw_invite.setdefault("event_id", None)
            raw_invite.setdefault("origin_server_ts", int(time() * 1000))

            invite = StateEvent.deserialize(raw_invite)
            invite.unsigned.invite_room_state = [StrippedStateEvent.deserialize(raw_event)
                                                 for raw_event in events
                                                 if raw_event != raw_invite]
            tasks += self.dispatch_event(invite, source=SyncStream.INVITED_ROOM | SyncStream.STATE)
        for room_id, room_data in rooms.get("leave", {}).items():
            for raw_event in room_data.get("timeline", {}).get("events", []):
                if "state_key" in raw_event:
                    raw_event["room_id"] = room_id
                    tasks += self.dispatch_event(StateEvent.deserialize(raw_event),
                                                 source=SyncStream.LEFT_ROOM | SyncStream.TIMELINE)
        return tasks

    def start(self, filter_data: Optional[Union[FilterID, Filter]]) -> asyncio.Future:
        """
        Start syncing with the server. Can be stopped with :meth:`stop`.

        Args:
            filter_data: The filter data or filter ID to use for syncing.
        """
        if self.syncing_task is not None:
            self.syncing_task.cancel()
        self.syncing_task = self.loop.create_task(self._try_start(filter_data))
        return self.syncing_task

    async def _try_start(self, filter_data: Optional[Union[FilterID, Filter]]) -> None:
        try:
            if isinstance(filter_data, Filter):
                filter_data = await self.create_filter(filter_data)
            await self._start(filter_data)
        except asyncio.CancelledError:
            self.log.debug("Syncing cancelled")
        except Exception as e:
            self.log.exception("Fatal error while syncing")
            await self.run_internal_event(InternalEventType.SYNC_STOPPED, error=e)
            return
        else:
            self.log.debug("Syncing stopped")
        await self.run_internal_event(InternalEventType.SYNC_STOPPED, error=None)

    async def _start(self, filter_id: Optional[FilterID]) -> None:
        fail_sleep = 5
        is_first = True

        self.log.debug("Starting syncing")
        next_batch = await self.sync_store.get_next_batch()
        await self.run_internal_event(InternalEventType.SYNC_STARTED)
        while True:
            try:
                data = await self.sync(since=next_batch, filter_id=filter_id,
                                       set_presence=self.presence)
                fail_sleep = 5
            except (asyncio.CancelledError, MUnknownToken):
                raise
            except Exception as e:
                self.log.exception(f"Sync request errored, waiting {fail_sleep}"
                                   " seconds before continuing")
                await self.run_internal_event(InternalEventType.SYNC_ERRORED, error=e,
                                              sleep_for=fail_sleep)
                await asyncio.sleep(fail_sleep, loop=self.loop)
                if fail_sleep < 320:
                    fail_sleep *= 2
                continue

            is_initial = not next_batch
            data["net.maunium.mautrix"] = {
                "is_initial": is_initial,
                "is_first": is_first,
            }
            next_batch = data.get("next_batch")
            await self.sync_store.put_next_batch(next_batch)
            await self.run_internal_event(InternalEventType.SYNC_SUCCESSFUL, data=data)
            if (self.ignore_first_sync and is_first) or (self.ignore_initial_sync and is_initial):
                is_first = False
                continue
            is_first = False
            try:
                tasks = self.handle_sync(data)
                await asyncio.gather(*tasks)
            except Exception:
                self.log.exception("Sync handling errored")

    def stop(self) -> None:
        """
        Stop a sync started with :meth:`start`.
        """
        if self.syncing_task:
            self.syncing_task.cancel()
            self.syncing_task = None

    @abstractmethod
    async def create_filter(self, filter_params: Filter) -> FilterID:
        pass

    @abstractmethod
    async def sync(self, since: SyncToken = None, timeout: int = 30000, filter_id: FilterID = None,
                   full_state: bool = False, set_presence: PresenceState = None) -> JSON:
        pass