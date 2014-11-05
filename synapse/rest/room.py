# -*- coding: utf-8 -*-
# Copyright 2014 OpenMarket Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

""" This module contains REST servlets to do with rooms: /rooms/<paths> """
from twisted.internet import defer

from base import RestServlet, client_path_pattern
from synapse.api.errors import SynapseError, Codes
from synapse.streams.config import PaginationConfig
from synapse.api.events.room import RoomMemberEvent, RoomRedactionEvent
from synapse.api.constants import Membership

import json
import logging
import urllib


logger = logging.getLogger(__name__)


class RoomCreateRestServlet(RestServlet):
    # No PATTERN; we have custom dispatch rules here

    def register(self, http_server):
        PATTERN = "/createRoom"
        register_txn_path(self, PATTERN, http_server)
        # define CORS for all of /rooms in RoomCreateRestServlet for simplicity
        http_server.register_path("OPTIONS",
                                  client_path_pattern("/rooms(?:/.*)?$"),
                                  self.on_OPTIONS)
        # define CORS for /createRoom[/txnid]
        http_server.register_path("OPTIONS",
                                  client_path_pattern("/createRoom(?:/.*)?$"),
                                  self.on_OPTIONS)

    @defer.inlineCallbacks
    def on_PUT(self, request, txn_id):
        try:
            defer.returnValue(
                self.txns.get_client_transaction(request, txn_id)
            )
        except KeyError:
            pass

        response = yield self.on_POST(request)

        self.txns.store_client_transaction(request, txn_id, response)
        defer.returnValue(response)

    @defer.inlineCallbacks
    def on_POST(self, request):
        auth_user = yield self.auth.get_user_by_req(request)

        room_config = self.get_room_config(request)
        info = yield self.make_room(room_config, auth_user, None)
        room_config.update(info)
        defer.returnValue((200, info))

    @defer.inlineCallbacks
    def make_room(self, room_config, auth_user, room_id):
        handler = self.handlers.room_creation_handler
        info = yield handler.create_room(
            user_id=auth_user.to_string(),
            room_id=room_id,
            config=room_config
        )
        defer.returnValue(info)

    def get_room_config(self, request):
        try:
            user_supplied_config = json.loads(request.content.read())
            if "visibility" not in user_supplied_config:
                # default visibility
                user_supplied_config["visibility"] = "public"
            return user_supplied_config
        except (ValueError, TypeError):
            raise SynapseError(400, "Body must be JSON.",
                               errcode=Codes.BAD_JSON)

    def on_OPTIONS(self, request):
        return (200, {})


# TODO: Needs unit testing for generic events
class RoomStateEventRestServlet(RestServlet):
    def register(self, http_server):
        # /room/$roomid/state/$eventtype
        no_state_key = "/rooms/(?P<room_id>[^/]*)/state/(?P<event_type>[^/]*)$"

        # /room/$roomid/state/$eventtype/$statekey
        state_key = ("/rooms/(?P<room_id>[^/]*)/state/"
                     "(?P<event_type>[^/]*)/(?P<state_key>[^/]*)$")

        http_server.register_path("GET",
                                  client_path_pattern(state_key),
                                  self.on_GET)
        http_server.register_path("PUT",
                                  client_path_pattern(state_key),
                                  self.on_PUT)
        http_server.register_path("GET",
                                  client_path_pattern(no_state_key),
                                  self.on_GET_no_state_key)
        http_server.register_path("PUT",
                                  client_path_pattern(no_state_key),
                                  self.on_PUT_no_state_key)

    def on_GET_no_state_key(self, request, room_id, event_type):
        return self.on_GET(request, room_id, event_type, "")

    def on_PUT_no_state_key(self, request, room_id, event_type):
        return self.on_PUT(request, room_id, event_type, "")

    @defer.inlineCallbacks
    def on_GET(self, request, room_id, event_type, state_key):
        user = yield self.auth.get_user_by_req(request)

        msg_handler = self.handlers.message_handler
        data = yield msg_handler.get_room_data(
            user_id=user.to_string(),
            room_id=urllib.unquote(room_id),
            event_type=urllib.unquote(event_type),
            state_key=urllib.unquote(state_key),
        )

        if not data:
            raise SynapseError(
                404, "Event not found.", errcode=Codes.NOT_FOUND
            )
        defer.returnValue((200, data[0].get_dict()["content"]))

    @defer.inlineCallbacks
    def on_PUT(self, request, room_id, event_type, state_key):
        user = yield self.auth.get_user_by_req(request)
        event_type = urllib.unquote(event_type)

        content = _parse_json(request)

        event = self.event_factory.create_event(
            etype=urllib.unquote(event_type),
            content=content,
            room_id=urllib.unquote(room_id),
            user_id=user.to_string(),
            state_key=urllib.unquote(state_key)
            )
        if event_type == RoomMemberEvent.TYPE:
            # membership events are special
            handler = self.handlers.room_member_handler
            yield handler.change_membership(event)
            defer.returnValue((200, {}))
        else:
            # store random bits of state
            msg_handler = self.handlers.message_handler
            yield msg_handler.store_room_data(
                event=event
            )
            defer.returnValue((200, {}))


# TODO: Needs unit testing for generic events + feedback
class RoomSendEventRestServlet(RestServlet):

    def register(self, http_server):
        # /rooms/$roomid/send/$event_type[/$txn_id]
        PATTERN = ("/rooms/(?P<room_id>[^/]*)/send/(?P<event_type>[^/]*)")
        register_txn_path(self, PATTERN, http_server, with_get=True)

    @defer.inlineCallbacks
    def on_POST(self, request, room_id, event_type):
        user = yield self.auth.get_user_by_req(request)
        content = _parse_json(request)

        event = self.event_factory.create_event(
            etype=urllib.unquote(event_type),
            room_id=urllib.unquote(room_id),
            user_id=user.to_string(),
            content=content
        )

        msg_handler = self.handlers.message_handler
        yield msg_handler.send_message(event)

        defer.returnValue((200, {"event_id": event.event_id}))

    def on_GET(self, request, room_id, event_type, txn_id):
        return (200, "Not implemented")

    @defer.inlineCallbacks
    def on_PUT(self, request, room_id, event_type, txn_id):
        try:
            defer.returnValue(
                self.txns.get_client_transaction(request, txn_id)
            )
        except KeyError:
            pass

        response = yield self.on_POST(request, room_id, event_type)

        self.txns.store_client_transaction(request, txn_id, response)
        defer.returnValue(response)


# TODO: Needs unit testing for room ID + alias joins
class JoinRoomAliasServlet(RestServlet):

    def register(self, http_server):
        # /join/$room_identifier[/$txn_id]
        PATTERN = ("/join/(?P<room_identifier>[^/]*)")
        register_txn_path(self, PATTERN, http_server)

    @defer.inlineCallbacks
    def on_POST(self, request, room_identifier):
        user = yield self.auth.get_user_by_req(request)

        # the identifier could be a room alias or a room id. Try one then the
        # other if it fails to parse, without swallowing other valid
        # SynapseErrors.

        identifier = None
        is_room_alias = False
        try:
            identifier = self.hs.parse_roomalias(
                urllib.unquote(room_identifier)
            )
            is_room_alias = True
        except SynapseError:
            identifier = self.hs.parse_roomid(
                urllib.unquote(room_identifier)
            )

        # TODO: Support for specifying the home server to join with?

        if is_room_alias:
            handler = self.handlers.room_member_handler
            ret_dict = yield handler.join_room_alias(user, identifier)
            defer.returnValue((200, ret_dict))
        else:  # room id
            event = self.event_factory.create_event(
                etype=RoomMemberEvent.TYPE,
                content={"membership": Membership.JOIN},
                room_id=urllib.unquote(identifier.to_string()),
                user_id=user.to_string(),
                state_key=user.to_string()
            )
            handler = self.handlers.room_member_handler
            yield handler.change_membership(event)
            defer.returnValue((200, {}))

    @defer.inlineCallbacks
    def on_PUT(self, request, room_identifier, txn_id):
        try:
            defer.returnValue(
                self.txns.get_client_transaction(request, txn_id)
            )
        except KeyError:
            pass

        response = yield self.on_POST(request, room_identifier)

        self.txns.store_client_transaction(request, txn_id, response)
        defer.returnValue(response)


# TODO: Needs unit testing
class PublicRoomListRestServlet(RestServlet):
    PATTERN = client_path_pattern("/publicRooms$")

    @defer.inlineCallbacks
    def on_GET(self, request):
        handler = self.handlers.room_list_handler
        data = yield handler.get_public_room_list()
        defer.returnValue((200, data))


# TODO: Needs unit testing
class RoomMemberListRestServlet(RestServlet):
    PATTERN = client_path_pattern("/rooms/(?P<room_id>[^/]*)/members$")

    @defer.inlineCallbacks
    def on_GET(self, request, room_id):
        # TODO support Pagination stream API (limit/tokens)
        user = yield self.auth.get_user_by_req(request)
        handler = self.handlers.room_member_handler
        members = yield handler.get_room_members_as_pagination_chunk(
            room_id=urllib.unquote(room_id),
            user_id=user.to_string())

        for event in members["chunk"]:
            # FIXME: should probably be state_key here, not user_id
            target_user = self.hs.parse_userid(event["user_id"])
            # Presence is an optional cache; don't fail if we can't fetch it
            try:
                presence_handler = self.handlers.presence_handler
                presence_state = yield presence_handler.get_state(
                    target_user=target_user, auth_user=user
                )
                event["content"].update(presence_state)
            except:
                pass

        defer.returnValue((200, members))


# TODO: Needs unit testing
class RoomMessageListRestServlet(RestServlet):
    PATTERN = client_path_pattern("/rooms/(?P<room_id>[^/]*)/messages$")

    @defer.inlineCallbacks
    def on_GET(self, request, room_id):
        user = yield self.auth.get_user_by_req(request)
        pagination_config = PaginationConfig.from_request(request)
        with_feedback = "feedback" in request.args
        handler = self.handlers.message_handler
        msgs = yield handler.get_messages(
            room_id=urllib.unquote(room_id),
            user_id=user.to_string(),
            pagin_config=pagination_config,
            feedback=with_feedback)

        defer.returnValue((200, msgs))


# TODO: Needs unit testing
class RoomStateRestServlet(RestServlet):
    PATTERN = client_path_pattern("/rooms/(?P<room_id>[^/]*)/state$")

    @defer.inlineCallbacks
    def on_GET(self, request, room_id):
        user = yield self.auth.get_user_by_req(request)
        handler = self.handlers.message_handler
        # Get all the current state for this room
        events = yield handler.get_state_events(
            room_id=urllib.unquote(room_id),
            user_id=user.to_string(),
        )
        defer.returnValue((200, events))


# TODO: Needs unit testing
class RoomInitialSyncRestServlet(RestServlet):
    PATTERN = client_path_pattern("/rooms/(?P<room_id>[^/]*)/initialSync$")

    @defer.inlineCallbacks
    def on_GET(self, request, room_id):
        yield self.auth.get_user_by_req(request)
        # TODO: Get all the initial sync data for this room and return in the
        # same format as initial sync, that is:
        # {
        #   membership: join,
        #   messages: [
        #       chunk: [ msg events ],
        #       start: s_tok,
        #       end: e_tok
        #   ],
        #   room_id: foo,
        #   state: [
        #       { state event } , { state event }
        #   ]
        # }
        # Probably worth keeping the keys room_id and membership for parity
        # with /initialSync even though they must be joined to sync this and
        # know the room ID, so clients can reuse the same code (room_id and
        # membership are MANDATORY for /initialSync, so the code will expect
        # it to be there)
        defer.returnValue((200, {}))


class RoomTriggerBackfill(RestServlet):
    PATTERN = client_path_pattern("/rooms/(?P<room_id>[^/]*)/backfill$")

    @defer.inlineCallbacks
    def on_GET(self, request, room_id):
        remote_server = urllib.unquote(request.args["remote"][0])
        room_id = urllib.unquote(room_id)
        limit = int(request.args["limit"][0])

        handler = self.handlers.federation_handler
        events = yield handler.backfill(remote_server, room_id, limit)

        res = [self.hs.serialize_event(event) for event in events]
        defer.returnValue((200, res))


# TODO: Needs unit testing
class RoomMembershipRestServlet(RestServlet):

    def register(self, http_server):
        # /rooms/$roomid/[invite|join|leave]
        PATTERN = ("/rooms/(?P<room_id>[^/]*)/"
                   "(?P<membership_action>join|invite|leave|ban|kick)")
        register_txn_path(self, PATTERN, http_server)

    @defer.inlineCallbacks
    def on_POST(self, request, room_id, membership_action):
        user = yield self.auth.get_user_by_req(request)

        content = _parse_json(request)

        # target user is you unless it is an invite
        state_key = user.to_string()
        if membership_action in ["invite", "ban", "kick"]:
            if "user_id" not in content:
                raise SynapseError(400, "Missing user_id key.")
            state_key = content["user_id"]

            if membership_action == "kick":
                membership_action = "leave"

        event = self.event_factory.create_event(
            etype=RoomMemberEvent.TYPE,
            content={"membership": unicode(membership_action)},
            room_id=urllib.unquote(room_id),
            user_id=user.to_string(),
            state_key=state_key
        )
        handler = self.handlers.room_member_handler
        yield handler.change_membership(event)
        defer.returnValue((200, {}))

    @defer.inlineCallbacks
    def on_PUT(self, request, room_id, membership_action, txn_id):
        try:
            defer.returnValue(
                self.txns.get_client_transaction(request, txn_id)
            )
        except KeyError:
            pass

        response = yield self.on_POST(request, room_id, membership_action)

        self.txns.store_client_transaction(request, txn_id, response)
        defer.returnValue(response)


class RoomRedactEventRestServlet(RestServlet):
    def register(self, http_server):
        PATTERN = ("/rooms/(?P<room_id>[^/]*)/redact/(?P<event_id>[^/]*)")
        register_txn_path(self, PATTERN, http_server)

    @defer.inlineCallbacks
    def on_POST(self, request, room_id, event_id):
        user = yield self.auth.get_user_by_req(request)
        content = _parse_json(request)

        event = self.event_factory.create_event(
            etype=RoomRedactionEvent.TYPE,
            room_id=urllib.unquote(room_id),
            user_id=user.to_string(),
            content=content,
            redacts=urllib.unquote(event_id),
        )

        msg_handler = self.handlers.message_handler
        yield msg_handler.send_message(event)

        defer.returnValue((200, {"event_id": event.event_id}))

    @defer.inlineCallbacks
    def on_PUT(self, request, room_id, event_id, txn_id):
        try:
            defer.returnValue(
                self.txns.get_client_transaction(request, txn_id)
            )
        except KeyError:
            pass

        response = yield self.on_POST(request, room_id, event_id)

        self.txns.store_client_transaction(request, txn_id, response)
        defer.returnValue(response)


def _parse_json(request):
    try:
        content = json.loads(request.content.read())
        if type(content) != dict:
            raise SynapseError(400, "Content must be a JSON object.",
                               errcode=Codes.NOT_JSON)
        return content
    except ValueError:
        raise SynapseError(400, "Content not JSON.", errcode=Codes.NOT_JSON)


def register_txn_path(servlet, regex_string, http_server, with_get=False):
    """Registers a transaction-based path.

    This registers two paths:
        PUT regex_string/$txnid
        POST regex_string

    Args:
        regex_string (str): The regex string to register. Must NOT have a
        trailing $ as this string will be appended to.
        http_server : The http_server to register paths with.
        with_get: True to also register respective GET paths for the PUTs.
    """
    http_server.register_path(
        "POST",
        client_path_pattern(regex_string + "$"),
        servlet.on_POST
    )
    http_server.register_path(
        "PUT",
        client_path_pattern(regex_string + "/(?P<txn_id>[^/]*)$"),
        servlet.on_PUT
    )
    if with_get:
        http_server.register_path(
            "GET",
            client_path_pattern(regex_string + "/(?P<txn_id>[^/]*)$"),
            servlet.on_GET
        )


def register_servlets(hs, http_server):
    RoomStateEventRestServlet(hs).register(http_server)
    RoomCreateRestServlet(hs).register(http_server)
    RoomMemberListRestServlet(hs).register(http_server)
    RoomMessageListRestServlet(hs).register(http_server)
    JoinRoomAliasServlet(hs).register(http_server)
    RoomTriggerBackfill(hs).register(http_server)
    RoomMembershipRestServlet(hs).register(http_server)
    RoomSendEventRestServlet(hs).register(http_server)
    PublicRoomListRestServlet(hs).register(http_server)
    RoomStateRestServlet(hs).register(http_server)
    RoomInitialSyncRestServlet(hs).register(http_server)
    RoomRedactEventRestServlet(hs).register(http_server)
