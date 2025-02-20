"""
Licensed to the Apache Software Foundation (ASF) under one
or more contributor license agreements.  See the NOTICE file
distributed with this work for additional information
regarding copyright ownership.  The ASF licenses this file
to you under the Apache License, Version 2.0 (the
"License"); you may not use this file except in compliance
with the License.  You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing,
software distributed under the License is distributed on an
"AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
KIND, either express or implied.  See the License for the
specific language governing permissions and limitations
under the License.
"""
import collections
import logging
import pytz
import datetime
from abc import abstractmethod

from marshmallow import Schema, fields, post_load

from yosai.core import (
    MapContext,
    ExpiredSessionException,
    InvalidArgumentException,
    IllegalStateException,
    InvalidSessionException,
    RandomSessionIDGenerator,
    SimpleIdentifierCollection,
    SessionCacheException,
    SessionCreationException,
    SessionEventException,
    StoppedSessionException,
    UnknownSessionException,
    session_settings,
    cache_abcs,
    event_abcs,
    serialize_abcs,
    session_abcs,
)

logger = logging.getLogger(__name__)


class AbstractSessionStore(session_abcs.SessionStore):
    """
    An abstract SessionStore implementation performs some sanity checks on
    session creation and reading and allows for pluggable Session ID generation
    strategies if desired.  The SessionStore.update and SessionStore.delete method
    implementations are left to subclasses.

    Session ID Generation
    ---------------------
    This class also allows for plugging in a SessionIdGenerator for
    custom ID generation strategies.  This is optional, as the default
    generator is probably sufficient for most cases.  Subclass implementations
    that do use a generator (default or custom) will want to call the
    generate_session_id(Session) method from within their do_create
    implementations.

    Subclass implementations that rely on the EIS data store to generate the ID
    automatically (e.g. when the session ID is also an auto-generated primary
    key), they can simply ignore the SessionIdGenerator concept
    entirely and just return the data store's ID from the do_create
    implementation.
    """

    def __init__(self):
        # shiro defaults to UUID where as yosai.core.uses well hashed urandom:
        self.session_id_generator = RandomSessionIDGenerator()

    def generate_session_id(self, session):
        """
        :param session: the new session instance for which an ID will be
                        generated and then assigned
        """
        try:
            return self.session_id_generator.generate_id(session)
        except AttributeError:
            msg = "session_id_generator attribute has not been configured"
            raise IllegalStateException(msg)

    def create(self, session):
        session_id = self._do_create(session)
        self.verify_session_id(session_id)
        return session_id

    def verify_session_id(self, session_id):
        if (session_id is None):
            msg = ("session_id returned from do_create implementation "
                   "is None. Please verify the implementation.")
            raise IllegalStateException(msg)

    def assign_session_id(self, session, session_id):
        if session is None or session_id is None:
            msg = ("session and sessionid parameters must be passed in "
                   "order to assign session_id")
            raise InvalidArgumentException(msg)
        session.session_id = session_id

    def read(self, session_id):
        session = self._do_read(session_id)
        if session is None:
            msg = "There is no session with id [" + str(session_id) + "]"
            raise UnknownSessionException(msg)
        return session

    @abstractmethod
    def _do_read(self, session_id):
        pass

    @abstractmethod
    def _do_create(self, session):
        pass


class MemorySessionStore(AbstractSessionStore):
    """
    Simple memory-based implementation of the SessionStore that stores all of its
    sessions in an in-memory dict.  This implementation does not page
    to disk and is therefore unsuitable for applications that could experience
    a large amount of sessions and would therefore result in MemoryError
    exceptions as the interpreter runs out of memory.  This class is *not*
    recommended for production use in most environments.

    Memory Restrictions
    -------------------
    If your application is expected to host many sessions beyond what can be
    stored in the memory available to the Python interpreter, it is highly
    recommended that you use a different SessionStore implementation using a
    more expansive or permanent backing data store.

    Instead, use a custom CachingSessionStore implementation that communicates
    with a higher-capacity data store of your choice (Redis, Memcached,
    file system, rdbms, etc).
    """

    def __init__(self):
        self.sessions = {}

    def update(self, session):
        return self.store_session(session.session_id, session)

    def delete(self, session):
        try:
            sessionid = session.session_id
            self.sessions.pop(sessionid)
        except AttributeError:
            msg = 'MemorySessionStore.delete None param passed'
            raise InvalidArgumentException(msg)
        except KeyError:
            msg = ('MemorySessionStore could not delete ', str(sessionid),
                   'because it does not exist in memory!')
            logger.warning(msg)

    def store_session(self, session_id, session):
        # stores only if session doesn't already exist, returning the existing
        # session (as default) otherwise
        if session_id is None or session is None:
            msg = 'MemorySessionStore.store_session invalid param passed'
            raise InvalidArgumentException(msg)

        return self.sessions.setdefault(session_id, session)

    def _do_create(self, session):
        sessionid = self.generate_session_id(session)
        self.assign_session_id(session, sessionid)
        self.store_session(sessionid, session)
        return sessionid

    def _do_read(self, sessionid):
        return self.sessions.get(sessionid)


class CachingSessionStore(AbstractSessionStore, cache_abcs.CacheHandlerAware):
    """
    An CachingSessionStore is a SessionStore that provides a transparent caching
    layer between the components that use it and the underlying EIS
    (Enterprise Information System) session backing store (e.g.
    Redis, Memcached, filesystem, database, enterprise grid/cloud, etc).

    Yosai omits 'active sessions' related functionality, which is used in Shiro
    as a means to bulk-invalidate timed out sessions.  Rather than manually sift
    through a collection containing every active session just to find
    timeouts, Yosai lazy-invalidates idle-timeout sessions and relies on
    automatic expiration of absolute timeout within cache. Absolute timeout is
    set as the cache entry's expiration time.

    Unlike Shiro:
    - Yosai implements the CRUD operations within CachingSessionStore
    rather than defer implementation further to subclasses
    - Yosai comments out support for a write-through caching strategy
    - Yosai uses an IdentifierCollection with session caching as part of its
      caching strategy


    Write-Through Caching
    -----------------------
    Write-through caching is a caching pattern where writes to the cache cause
    writes to an underlying database (EIS). The cache acts as a facade to the
    underlying resource.

    All methods within CachingSessionStore are implemented to employ caching
    behavior while delegating cache write-through related operations
    to respective 'do' CRUD methods, which are to be implemented by subclasses:
    do_create, do_read, do_update and do_delete.

    Potential write-through caching strategies:
    ------------------------------------
    As of Postgresql 9.5, you can UPSERT session records

    Databases such as Postgresql offer what is known as foreign data wrappers
    (FDWs) that pipe data from cache to the database.

    Ref: https://en.wikipedia.org/wiki/Cache_%28computing%29#Writing_policies

    """

    def __init__(self):
        super().__init__()  # obtains a session id generator
        self._cache_handler = None

    # cache_handler property is required for CacheHandlerAware interface
    @property
    def cache_handler(self):
        return self._cache_handler

    # cache_handler property is required for CacheHandlerAware interface
    @cache_handler.setter
    def cache_handler(self, cachehandler):
        self._cache_handler = cachehandler

    def create(self, session):
        """
        caches the session and caches an entry to associate the cached session
        with the subject
        """
        sessionid = super().create(session)
        self._cache(session, sessionid)
        self._cache_identifiers_to_key_map(session, sessionid)
        return sessionid

    def read(self, sessionid):
        session = self._get_cached_session(sessionid)

        # for write-through caching:
        # if (session is None):
        #    session = super().read(sessionid)

        return session

    def update(self, session, update_identifiers_map):

        # for write-through caching:
        # self._do_update(session)

        if (session.is_valid):
            self._cache(session, session.session_id)

            if update_identifiers_map:
                self._cache_identifiers_to_key_map(session, session.session_id)
        else:
            self._uncache(session)

    def delete(self, session):
        self._uncache(session)
        # for write-through caching:
        # self._do_delete(session)

    # java overloaded methods combined:
    def _get_cached_session(self, sessionid):
        try:
            # assume that sessionid isn't None

            return self.cache_handler.get(domain='session',
                                          identifier=sessionid)
        except AttributeError:
            msg = "no cache parameter nor lazy-defined cache"
            logger.warning(msg)

        return None

    def _cache_identifiers_to_key_map(self, session, session_id):
        """
        When a session is associated with a user, it will have an identifiers
        attribute.  This method creates a cache entry within a user's cache space
        that is used to identify the active session associated with the user.

        using the primary identifier within the key is new to yosai
        """
        isk = 'identifiers_session_key'
        identifiers = session.get_internal_attribute(isk)

        try:
            self.cache_handler.set(domain='session',
                                   identifier=identifiers.primary_identifier,
                                   value=DefaultSessionKey(session_id))
        except AttributeError:
            msg = "Could not cache identifiers_session_key."
            if not identifiers:
                msg += '  \'identifiers\' internal attribute not set.'
            logger.debug(msg)

    def _cache(self, session, session_id):

        try:
            self.cache_handler.set(domain='session',
                                   identifier=session_id,
                                   value=session)
        except AttributeError:
            msg = "Cannot cache without a cache_handler."
            raise SessionCacheException(msg)

    def _uncache(self, session):

        try:
            sessionid = session.session_id

            # delete the serialized session object:
            self.cache_handler.delete(domain='session',
                                      identifier=sessionid)

            try:
                identifiers = session.get_internal_attribute('identifiers_session_key')
                primary_id = identifiers.primary_identifier
                # delete the mapping between a user and session id:
                self.cache_handler.delete(domain='session',
                                          identifier=primary_id)
            except AttributeError:
                msg = '_uncache: Could not obtain identifiers from session'
                logger.warn(msg)

        except AttributeError:
            msg = "Cannot uncache without a cache_handler."
            raise SessionCacheException(msg)

    def _do_create(self, session):
        sessionid = self.generate_session_id(session)
        self.assign_session_id(session, sessionid)
        return sessionid

    # intended for write-through caching:
    def _do_read(self, session_id):
        pass

    # intended for write-through caching:
    def _do_delete(self, session):
        pass

    # intended for write-through caching:
    def _do_update(self, session):
        pass


# Yosai omits the SessionListenerAdapter class

class ProxiedSession(session_abcs.Session):
    """
    Simple Session implementation that immediately delegates all
    corresponding calls to an underlying proxied session instance.

    This class is mostly useful for framework subclassing to intercept certain
    Session calls and perform additional logic.
    """

    def __init__(self, target_session):
        """
        unlike shiro, yosai differentiates session attributes for internal use
        from session attributes for external use
        """
        # the proxied instance:
        self._delegate = target_session
        self._session_id = None

    @property
    def session_id(self):
        return self._delegate.session_id

    @property
    def start_timestamp(self):
        return self._delegate.start_timestamp

    @property
    def last_access_time(self):
        return self._delegate.last_access_time

    @property
    def idle_timeout(self):
        return self._delegate.idle_timeout

    @idle_timeout.setter
    def idle_timeout(self, max_idle_time):
        self._delegate.idle_timeout = max_idle_time

    @property
    def absolute_timeout(self):
        return self._delegate.absolute_timeout

    @absolute_timeout.setter
    def absolute_timeout(self, abs_time):
        self._delegate.absolute_timeout = abs_time

    @property
    def host(self):
        return self._delegate.host

    def touch(self):
        self._delegate.touch()

    def stop(self, identifiers):
        self._delegate.stop(identifiers)

    @property
    def attribute_keys(self):
        return self._delegate.attribute_keys

    @property
    def internal_attribute_keys(self):
        return self._delegate.internal_attribute_keys

    def get_internal_attribute(self, key):
        return self._delegate.get_internal_attribute(key)

    def set_internal_attribute(self, key, value):
        self._delegate.set_internal_attribute(key, value)

    def remove_internal_attribute(self, key):
        self._delegate.remove_internal_attribute(key)

    def get_attribute(self, key):
        return self._delegate.get_attribute(key)

    # new to yosai
    def get_attributes(self, attributes):
        """
        :param attributes: the keys of attributes to get from the session
        :type attributes: list of strings

        :returns: a dict containing the attributes requested
        """
        return self._delegate.get_attributes(attributes)

    def set_attribute(self, key, value):
        self._delegate.set_attribute(key, value)

    # new to yosai
    def set_attributes(self, attributes):
        """
        :param attributes: the attributes to add to the session
        :type attributes: dict
        """
        self._delegate.set_attributes(attributes)

    def remove_attribute(self, key):
        self._delegate.remove_attribute(key)  # you could validate here

    # new to yosai
    def remove_attributes(self, keys):
        """
        :param attributes: the keys of attributes to remove from the session
        :type attributes: list of strings
        """
        self._delegate.remove_attributes(keys)  # you could validate here

    def __repr__(self):
        return "ProxiedSession(session_id={0}, attributes={1})".format(
            self.session_id, self.attribute_keys)

# removed ImmutableProxiedSession because it can't be sent over the eventbus

class SimpleSession(session_abcs.ValidatingSession,
                    serialize_abcs.Serializable):

    # Yosai omits:
    #    - the manually-managed class version control process (too policy-reliant)
    #    - the bit-flagging technique (will cross this bridge later, if needed)

    def __init__(self, host=None):
        self._attributes = {}
        self._internal_attributes = {}
        self._is_expired = None
        self._session_id = None

        self._stop_timestamp = None
        self._start_timestamp = datetime.datetime.now(pytz.utc)

        self._last_access_time = self._start_timestamp

        # yosai.core.renames global_session_timeout to idle_timeout and added
        # the absolute_timeout feature
        self._absolute_timeout = session_settings.absolute_timeout  # timedelta
        self._idle_timeout = session_settings.idle_timeout  # timedelta

        self._host = host

    # the properties are required to enforce the Session abc-interface..
    @property
    def absolute_timeout(self):
        return self._absolute_timeout

    @absolute_timeout.setter
    def absolute_timeout(self, abs_timeout):
        """
        :type abs_timeout: timedelta
        """
        self._absolute_timeout = abs_timeout

    @property
    def attributes(self):
        return self._attributes

    @property
    def attribute_keys(self):
        if (self.attributes is None):
            return None
        return set(self.attributes)  # a set of keys

    @property
    def internal_attributes(self):
        if not hasattr(self, '_internal_attributes'):
            self._internal_attributes = {}
        return self._internal_attributes

    @property
    def internal_attribute_keys(self):
        if (self.internal_attributes is None):
            return None
        return set(self.internal_attributes)  # a set of keys

    @property
    def host(self):
        return self._host

    @host.setter
    def host(self, host):
        """
        :type host:  string
        """
        self._host = host

    @property
    def idle_timeout(self):
        return self._idle_timeout

    @idle_timeout.setter
    def idle_timeout(self, idle_timeout):
        """
        :type idle_timeout: timedelta
        """
        self._idle_timeout = idle_timeout

    @property
    def is_expired(self):
        return self._is_expired

    @is_expired.setter
    def is_expired(self, expired):
        self._is_expired = expired

    @property
    def is_stopped(self):
        return bool(self.stop_timestamp)

    @property
    def last_access_time(self):
        return self._last_access_time

    @last_access_time.setter
    def last_access_time(self, last_access_time):
        """
        :param  last_access_time: time that the Session was last used, in utc
        :type last_access_time: datetime
        """
        self._last_access_time = last_access_time

    # DG:  renamed id to session_id because of reserved word conflict
    @property
    def session_id(self):
        return self._session_id

    @session_id.setter
    def session_id(self, identity):
        self._session_id = identity

    @property
    def start_timestamp(self):
        return self._start_timestamp

    @start_timestamp.setter
    def start_timestamp(self, start_ts):
        """
        :param  start_ts: the time that the Session is started, in utc
        :type start_ts: datetime
        """
        self._start_timestamp = start_ts

    @property
    def stop_timestamp(self):
        if not hasattr(self, '_stop_timestamp'):
            self._stop_timestamp = None
        return self._stop_timestamp

    @stop_timestamp.setter
    def stop_timestamp(self, stop_ts):
        """
        :param  stop_ts: the time that the Session is stopped, in utc
        :type stop_ts: datetime
        """
        self._stop_timestamp = stop_ts

    @property
    def absolute_expiration(self):
        if self.absolute_timeout:
            return self.start_timestamp + self.absolute_timeout
        return None

    @property
    def idle_expiration(self):
        if self.idle_timeout:
            return self.last_access_time + self.idle_timeout
        return None

    def touch(self):
        self.last_access_time = datetime.datetime.now(pytz.utc)

    def stop(self):
        if (not self.stop_timestamp):
            self.stop_timestamp = datetime.datetime.now(pytz.utc)

    def expire(self):
        self.stop()
        self.is_expired = True

    @property
    def is_valid(self):
        return (not self.is_stopped and not self.is_expired)

    def is_timed_out(self):
        """
        determines whether a Session has been inactive/idle for too long a time
        OR exceeds the absolute time that a Session may exist
        """
        if (self.is_expired):
            return True

        if (self.absolute_timeout or self.idle_timeout):
            if (not self.last_access_time):
                msg = ("session.last_access_time for session with id [" +
                       str(self.session_id) + "] is null. This value must be"
                       "set at least once, preferably at least upon "
                       "instantiation. Please check the " +
                       self.__class__.__name__ +
                       " implementation and ensure self value will be set "
                       "(perhaps in the constructor?)")
                raise IllegalStateException(msg)

            """
             Calculate at what time a session would have been last accessed
             for it to be expired at this point.  In other words, subtract
             from the current time the amount of time that a session can
             be inactive before expiring.  If the session was last accessed
             before this time, it is expired.
            """
            current_time = datetime.datetime.now(pytz.utc)

            # Check 1:  Absolute Timeout
            if self.absolute_expiration:
                if (current_time > self.absolute_expiration):
                    return True

            # Check 2:  Inactivity Timeout
            if self.idle_expiration:
                if (current_time > self.idle_expiration):
                    return True

        else:

            msg2 = ("Timeouts not set for session with id [" +
                    str(self.session_id) + "]. Session is not considered "
                    "expired.")
            logger.debug(msg2)

        return False

    def validate(self):
        # check for stopped:
        if (self.is_stopped):
            # timestamp is set, so the session is considered stopped:
            msg = ("Session with id [" + str(self.session_id) + "] has been "
                   "explicitly stopped.  No further interaction under "
                   "this session is allowed.")
            raise StoppedSessionException(msg)  # subclass of InvalidSessionException

        # check for expiration
        if (self.is_timed_out()):
            self.expire()

            # throw an exception explaining details of why it expired:
            lastaccesstime = self.last_access_time.isoformat()

            idle_timeout = self.idle_timeout.seconds
            idle_timeout_min = str(idle_timeout // 60)

            absolute_timeout = self.absolute_timeout.seconds
            absolute_timeout_min = str(absolute_timeout // 60)

            currenttime = datetime.datetime.now(pytz.utc).isoformat()
            session_id = str(self.session_id)

            msg2 = ("Session with id [" + session_id + "] has expired. "
                    "Last access time: " + lastaccesstime +
                    ".  Current time: " + currenttime +
                    ".  Session idle timeout is set to " + str(idle_timeout) +
                    " seconds (" + idle_timeout_min + " minutes) and "
                    " absolute timeout is set to " + str(absolute_timeout) +
                    " seconds (" + absolute_timeout_min + "minutes)")

            logger.debug(msg2)

            raise ExpiredSessionException(msg2)

    def get_internal_attribute(self, key):
        if (not self.internal_attributes):
            return None

        return self.internal_attributes.get(key)

    def set_internal_attribute(self, key, value=None):
        if (not value):
            self.remove_internal_attribute(key)
        else:
            self.internal_attributes[key] = value

    def remove_internal_attribute(self, key):
        if (not self.internal_attributes):
            return None
        else:
            return self.internal_attributes.pop(key, None)

    def get_attribute(self, key):
        return self.attributes.get(key)

    # new to yosai
    def get_attributes(self, keys):
        """
        :param attributes: the keys of attributes to get from the session
        :type attributes: list of strings

        :returns: a dict containing the attributes requested, if they exist
        """
        return {key: self.attributes.get(key) for key in keys if key in self.attributes}

    def set_attribute(self, key, value):
        self.attributes[key] = value

    # new to yosai is the bulk setting/getting/removing
    def set_attributes(self, attributes):
        """
        :param attributes: the attributes to add to the session
        :type attributes: dict
        """
        self.attributes.update(attributes)

    def remove_attribute(self, key):
        return self.attributes.pop(key, None)

    # new to yosai
    def remove_attributes(self, keys):
        """
        :param attributes: the keys of attributes to remove from the session
        :type attributes: list of strings

        :returns: a list of popped attribute values
        """
        return [self.attributes.pop(key, None) for key in keys]

    # deleted on_equals as it is unecessary in python
    # deleted hashcode method as python's __hash__ may be fine -- TBD!

    # omitting the bit-flagging methods:
    #       writeobject, readObject, getalteredfieldsbitmask, isFieldPresent

    def __eq__(self, other):
        if self is other:
            return True
        if isinstance(other, session_abcs.ValidatingSession):
            return (self._session_id == other._session_id and
                    self._idle_timeout == other._idle_timeout and
                    self._absolute_timeout == other._absolute_timeout and
                    self._start_timestamp == other._start_timestamp)
                    # self._is_expired == other._is_expired and
                    #self._last_access_time == other._last_access_time)

        return False

    def __repr__(self):
        return ("SimpleSession(session_id: {0}, start_timestamp: {1}, "
                "stop_timestamp: {2}, last_access_time: {3},"
                "idle_timeout: {4}, absolute_timeout: {5}, is_expired: {6},"
                "host: {7}, attributes:{8})".
                format(self.session_id, self.start_timestamp,
                       self.stop_timestamp, self.last_access_time,
                       self.idle_timeout, self.absolute_timeout,
                       self.is_expired, self.host, self._attributes))

    # the developer using Yosai must define the attribute schema:
    class AttributesSchema(Schema):
        pass

    @classmethod
    def set_attributes_schema(cls, schema):
        cls.AttributesSchema = schema

    @classmethod
    def serialization_schema(cls):

        class InternalSessionAttributesSchema(Schema):
            identifiers_session_key = fields.Nested(
                SimpleIdentifierCollection.serialization_schema(),
                attribute='identifiers_session_key',
                allow_none=False)

            authenticated_session_key = fields.Boolean(
                attribute='authenticated_session_key',
                allow_none=False)

            run_as_identifiers_session_key = fields.Nested(
                SimpleIdentifierCollection.serialization_schema(),
                attribute='run_as_identifiers_session_key',
                many=True,
                allow_none=False)

            @post_load
            def make_internal_attributes(self, data):
                try:
                    raisk = 'run_as_identifiers_session_key'
                    runas = data.get(raisk)
                    if runas:
                        que = collections.deque(runas)
                        data[raisk] = que
                except TypeError:
                    msg = ("Session de-serialization note: "
                           "run_as_identifiers_session_key attribute N/A.")
                    logger.warning(msg)

                return data

        class SerializationSchema(Schema):
            _session_id = fields.Str(allow_none=True)
            _start_timestamp = fields.DateTime(allow_none=True)  # iso is default
            _stop_timestamp = fields.DateTime(allow_none=True)  # iso is default
            _last_access_time = fields.DateTime(allow_none=True)  # iso is default
            _idle_timeout = fields.TimeDelta(allow_none=True)
            _absolute_timeout = fields.TimeDelta(allow_none=True)
            _is_expired = fields.Boolean(allow_none=True)
            _host = fields.Str(allow_none=True)

            # NOTE:  After you've defined your SimpleSessionAttributesSchema,
            #        the Raw() fields assignment below should be replaced by
            #        the Schema line that follows it
            _internal_attributes = fields.Nested(InternalSessionAttributesSchema,
                                                 allow_none=True)

            _attributes = fields.Nested(cls.AttributesSchema,
                                        allow_null=True)

            @post_load
            def make_simple_session(self, data):
                mycls = SimpleSession
                instance = mycls.__new__(mycls)
                instance.__dict__.update(data)

                return instance

        return SerializationSchema


class SimpleSessionFactory(session_abcs.SessionFactory):

    @classmethod
    def create_session(cls, session_context=None):
        return SimpleSession(host=getattr(session_context, 'host', None))


class DelegatingSession(session_abcs.Session):
    """
    A DelegatingSession is a client-tier representation of a server side
    Session.  This implementation is basically a proxy to a server-side
    NativeSessionManager, which will return the proper results for each
    method call.

    A DelegatingSession will cache data when appropriate to avoid a remote
    method invocation, only communicating with the server when necessary and
    if write-through session caching is implemented.

    Of course, if used in-process with a NativeSessionManager business object,
    as might be the case in a web-based application where the web classes
    and server-side business objects exist in the same namespace, a remote
    method call will not be incurred.

    """

    def __init__(self, session_manager, sessionkey):
        # omitting None-type checking
        self.session_key = sessionkey
        self.session_manager = session_manager
        self._start_timestamp = None
        self._host = None

    @property
    def session_id(self):
        return self.session_key.session_id

    @property
    def start_timestamp(self):
        if (not self._start_timestamp):
            self._start_timestamp = self.session_manager.get_start_timestamp(
                self.session_key)
        return self._start_timestamp

    @property
    def last_access_time(self):
        return self.session_manager.get_last_access_time(self.session_key)

    @property
    def idle_timeout(self):
        return self.session_manager.get_idle_timeout(self.session_key)

    @idle_timeout.setter
    def idle_timeout(self, timeout):
        self.session_manager.set_idle_timeout(self.session_key, timeout)

    @property
    def absolute_timeout(self):
        return self.session_manager.get_absolute_timeout(self.session_key)

    @absolute_timeout.setter
    def absolute_timeout(self, timeout):
        self.session_manager.set_absolute_timeout(self.session_key, timeout)

    @property
    def host(self):
        if (not self._host):
            self._host = self.session_manager.get_host(self.session_key)

        return self._host

    def touch(self):
        self.session_manager.touch(self.session_key)

    def stop(self, identifiers):
        self.session_manager.stop(self.session_key, identifiers)

    @property
    def internal_attribute_keys(self):
        return self.session_manager.get_internal_attribute_keys(self.session_key)

    def get_internal_attribute(self, attribute_key):
        return self.session_manager.get_internal_attribute(self.session_key,
                                                           attribute_key)

    def set_internal_attribute(self, attribute_key, value=None):
        if (value is None):
            self.remove_internal_attribute(attribute_key)
        else:
            self.session_manager.set_internal_attribute(self.session_key,
                                                        attribute_key,
                                                        value)

    def remove_internal_attribute(self, attribute_key):
        return self.session_manager.remove_internal_attribute(self.session_key,
                                                              attribute_key)

    @property
    def attribute_keys(self):
        return self.session_manager.get_attribute_keys(self.session_key)

    def get_attribute(self, attribute_key):
        if attribute_key:
            return self.session_manager.get_attribute(self.session_key,
                                                      attribute_key)
        return None

    def get_attributes(self, attribute_keys):
        if attribute_keys:
            return self.session_manager.get_attributes(self.session_key,
                                                       attribute_keys)
        return None

    def set_attribute(self, attribute_key, value):
        if all([attribute_key, value]):
            self.session_manager.set_attribute(self.session_key,
                                               attribute_key,
                                               value)

    def set_attributes(self, attributes):
        if attributes:
            self.session_manager.set_attributes(self.session_key, attributes)

    def remove_attribute(self, attribute_key):
        if attribute_key:
            return self.session_manager.remove_attribute(self.session_key,
                                                         attribute_key)

    def remove_attributes(self, attribute_keys):
        if attribute_keys:
            return self.session_manager.remove_attribute(self.session_key,
                                                         attribute_keys)

    def __repr__(self):
        return "DelegatingSession(session_id: {0})".format(self.session_id)


class DefaultSessionKey(session_abcs.SessionKey,
                        serialize_abcs.Serializable):

    def __init__(self, session_id):
        self._session_id = session_id

    @property
    def session_id(self):
        return self._session_id

    @session_id.setter
    def session_id(self, session_id):
        self._session_id = session_id

    def __eq__(self, other):
        try:
            return self.session_id == other.session_id
        except AttributeError:
            return False

    def __repr__(self):
        return "SessionKey(session_id={0})".format(self.session_id)

    @classmethod
    def serialization_schema(cls):
        class SerializationSchema(Schema):
            _session_id = fields.Str(allow_none=True)

            @post_load
            def make_default_session_key(self, data):
                mycls = DefaultSessionKey
                instance = mycls.__new__(mycls)
                instance.__dict__.update(data)
                return instance

        return SerializationSchema


# yosai.core.refactor:
class SessionEventHandler(event_abcs.EventBusAware):

    def __init__(self):
        self._event_bus = None  # setter injected

    @property
    def event_bus(self):
        return self._event_bus

    @event_bus.setter
    def event_bus(self, event_bus):
        self._event_bus = event_bus

    def notify_start(self, session):
        """
        :type session:  SimpleSession
        """
        try:
            self.event_bus.publish('SESSION.START', session_id=session.session_id)
        except AttributeError:
            msg = "Could not publish SESSION.START event"
            raise SessionEventException(msg)

    def notify_stop(self, session_tuple):
        """
        :type identifiers:  SimpleIdentifierCollection
        """
        try:
            self.event_bus.publish('SESSION.STOP', items=session_tuple)
        except AttributeError:
            msg = "Could not publish SESSION.STOP event"
            raise SessionEventException(msg)

    def notify_expiration(self, session_tuple):
        """
        :type identifiers:  SimpleIdentifierCollection
        """

        try:
            self.event_bus.publish('SESSION.EXPIRE', items=session_tuple)
        except AttributeError:
            msg = "Could not publish SESSION.EXPIRE event"
            raise SessionEventException(msg)


# 5 monopoly dollars to the person who helps me rename this:
class DefaultNativeSessionHandler(session_abcs.SessionHandler,
                                  event_abcs.EventBusAware):

    def __init__(self,
                 session_event_handler,
                 auto_touch=True,
                 session_store=CachingSessionStore(),
                 delete_invalid_sessions=True):
        self.delete_invalid_sessions = delete_invalid_sessions
        self._session_store = session_store
        self.session_event_handler = session_event_handler
        self.auto_touch = auto_touch
        self._cache_handler = None  # setter injected

    @property
    def session_store(self):
        return self._session_store

    @session_store.setter
    def session_store(self, sessionstore):
        self._session_store = sessionstore
        if self.cache_handler:
            self.apply_cache_handler_to_session_store()

    @property
    def cache_handler(self):
        return self._cache_handler

    @cache_handler.setter
    def cache_handler(self, cachehandler):
        self._cache_handler = cachehandler
        self.apply_cache_handler_to_session_store()

    def apply_cache_handler_to_session_store(self):
        try:
            if isinstance(self.session_store, cache_abcs.CacheHandlerAware):
                self.session_store.cache_handler = self._cache_handler
        except AttributeError:
            msg = ("tried to set a cache manager in a SessionStore that isn\'t"
                   "defined or configured in the DefaultNativeSessionManager")
            logger.warning(msg)
            return

    @property
    def event_bus(self):
        # just a pass-through
        return self.session_event_handler.event_bus

    @event_bus.setter
    def event_bus(self, eventbus):
        # pass-through
        self.session_event_handler.event_bus = eventbus

    # -------------------------------------------------------------------------
    # Session Creation Methods
    # -------------------------------------------------------------------------

    def create_session(self, session):
        """
        :returns: a session_id string
        """
        return self.session_store.create(session)

    # -------------------------------------------------------------------------
    # Session Teardown Methods
    # -------------------------------------------------------------------------

    def delete(self, session):
        self.session_store.delete(session)

    # -------------------------------------------------------------------------
    # Session Lookup Methods
    # -------------------------------------------------------------------------

    def _retrieve_session(self, session_key):
        """
        :type session_key: DefaultSessionKey
        :returns: SimpleSession
        """
        session_id = session_key.session_id
        if (session_id is None):
            msg = ("Unable to resolve session ID from SessionKey [{0}]."
                   "Returning null to indicate a session could not be "
                   "found.".format(session_key))
            logger.debug(msg)
            return None

        session = self.session_store.read(session_id)

        if (session is None):
            # session ID was provided, meaning one is expected to be found,
            # but we couldn't find one:
            msg2 = "Could not find session with ID [{0}]".format(session_id)
            raise UnknownSessionException(msg2)

        return session

    def do_get_session(self, session_key):
        """
        :type session_key: DefaultSessionKey
        :returns: SimpleSession
        """
        session_id = session_key.session_id
        msg = ("do_get_session: Attempting to retrieve session with key " +
               str(session_id))
        logger.debug(msg)

        session = self._retrieve_session(session_key)

        # first check whether valid and THEN touch it
        if (session is not None):
            self.validate(session, session_key)

            # won't be called unless the session is valid (due exceptions):
            if self.auto_touch:  # new to yosai
                session.touch()
                self.on_change(session)

        return session

    # -------------------------------------------------------------------------
    # Validation Methods
    # -------------------------------------------------------------------------

    def validate(self, session, session_key):
        # session exception hierarchy:  invalid -> stopped -> expired
        try:
            session.validate()  # can raise Stopped or Expired exceptions
        except AttributeError:  # means it's not a validating session
            msg = ("The {0} implementation only supports Validating "
                   "Session implementations of the {1} interface.  "
                   "Please either implement this interface in your "
                   "session implementation or override the {0}"
                   ".do_validate(Session) method to validate.").\
                format(self.__class__.__name__, 'ValidatingSession')

            raise IllegalStateException(msg)

        except ExpiredSessionException as ese:
            self.on_expiration(session, ese, session_key)
            raise ese

        # should be a stopped exception if this is reached, but a more
        # generalized invalid exception is checked
        except InvalidSessionException as ise:
            self.on_invalidation(session, ise, session_key)
            raise ise

    # -------------------------------------------------------------------------
    # Event-driven Methods
    # -------------------------------------------------------------------------

    # used by DefaultWebSessionManager:
    def on_start(self, session, session_context):
        """
        placeholder for subclasses to react to a new session being created
        """
        pass

    def on_stop(self, session):
        try:
            session.last_access_time = session.stop_timestamp
        except AttributeError:
            msg = "not working with a SimpleSession instance"
            logger.warning(msg)

        self.on_change(session)

    def after_stopped(self, session):
        # this appears to be redundant
        if (self.delete_invalid_sessions):
            self.delete(session)

    def on_expiration(self, session, expired_session_exception=None,
                      session_key=None):
        """
        This method overloaded for now (java port).  TBD
        Two possible scenarios supported:
            1) All three arguments passed = session + ese + session_key
            2) Only session passed as an argument
        """
        if (expired_session_exception and session_key):
            try:
                self.on_change(session)
                msg = "Session with id [{0}] has expired.".\
                    format(session.session_id)
                logger.debug(msg)

                identifiers = session.get_internal_attribute('identifiers_session_key')

                session_tuple = collections.namedtuple(
                    'session_tuple', ['identifiers', 'session_key'])
                mysession = session_tuple(identifiers, session_key)

                self.session_event_handler.notify_expiration(mysession)
            except:
                raise
            finally:
                self.after_expired(session)
        elif not expired_session_exception and not session_key:
            self.on_change(session)

        # Yosai adds this exception handling
        else:
            msg = "on_exception takes either 1 argument or 3 arguments"
            raise InvalidArgumentException(msg)

    def after_expired(self, session):
        if (self.delete_invalid_sessions):
            self.delete(session)

    def on_invalidation(self, session, ise, session_key):
        # session exception hierarchy:  invalid -> stopped -> expired
        if (isinstance(ise, ExpiredSessionException)):
            self.on_expiration(session, ise, session_key)
            return

        msg = "Session with id [{0}] is invalid.".format(session.session_id)
        logger.debug(msg)

        try:
            self.on_stop(session)
            identifiers = session.get_internal_attribute('identifiers_session_key')

            session_tuple = collections.namedtuple(
                'session_tuple', ['identifiers', 'session_key'])
            mysession = session_tuple(identifiers, session_key)

            self.session_event_handler.notify_stop(mysession)
        except:
            raise
        # DG:  this results in a redundant delete operation (from shiro):
        finally:
            self.after_stopped(session)

    def on_change(self, session, update_identifiers_map=False):
        if self.auto_touch and not session.is_stopped:  # new to yosai
            session.touch()

        self.session_store.update(session, update_identifiers_map)


class DefaultNativeSessionManager(cache_abcs.CacheHandlerAware,
                                  session_abcs.NativeSessionManager,
                                  event_abcs.EventBusAware):
    """
    Yosai's DefaultNativeSessionManager represents a massive refactoring of Shiro's
    SessionManager object model.  The refactoring is an ongoing effort to
    replace a confusing inheritance-based mixin object graph with a compositional
    design.  This compositional design continues to evolve.  Event handling can be
    better designed as it currently is done by the manager AND session handler.
    Pull Requests are welcome.

    Touching Sessions
    ------------------
    A session's last_access_time must be updated on every request.  Updating
    the last access timestamp is required for session validation to work
    correctly as the timestamp is used to determine whether a session has timed
    out due to inactivity.

    In web applications, the [Shiro Filter] updates the session automatically
    via the session.touch() method.  For non-web environments (e.g. for RMI),
    something else must call the touch() method to ensure the session
    validation logic functions correctly.

    Shiro does not enable auto-touch within the DefaultNativeSessionManager. It is not
     yet clear why Shiro doesn't.  Until the reason why is revealed, Yosai
     includes a new auto_touch feature to enable/disable auto-touching.

    """

    def __init__(self):
        self.session_factory = SimpleSessionFactory()
        self._session_event_handler = SessionEventHandler()
        self.session_handler =\
            DefaultNativeSessionHandler(session_event_handler=self.session_event_handler,
                                        auto_touch=True)
        self._event_bus = None

    @property
    def session_event_handler(self):
        return self._session_event_handler

    @session_event_handler.setter
    def session_event_handler(self, handler):
        self._session_event_handler = handler
        self.session_handler.session_event_handler = handler

    @property
    def cache_handler(self):
        return self.session_handler.cache_handler

    @cache_handler.setter
    def cache_handler(self, cachehandler):
        # no need for a local instance, just pass through
        self.session_handler.cache_handler = cachehandler

    @property
    def event_bus(self):
        return self._event_bus

    @event_bus.setter
    def event_bus(self, eventbus):
        self._event_bus = eventbus
        self.session_event_handler.event_bus = eventbus
        self.session_handler.event_bus = eventbus  # it passes through

    # -------------------------------------------------------------------------
    # Session Lifecycle Methods
    # -------------------------------------------------------------------------

    def start(self, session_context):
        """
        unlike shiro, yosai does not apply session timeouts from within the
        start method of the SessionManager but rather defers timeout settings
        responsibilities to the SimpleSession, which uses session_settings
        """
        # is a SimpleSesson:
        session = self._create_session(session_context)

        self.session_handler.on_start(session, session_context)

        self.session_event_handler.notify_start(session)

        # Don't expose the EIS-tier Session object to the client-tier, but
        # rather a DelegatingSession:
        return self.create_exposed_session(session=session, context=session_context)

    def stop(self, session_key, identifiers):
        session = self._lookup_required_session(session_key)
        try:
            msg = ("Stopping session with id [{0}]").format(session.session_id)
            logger.debug(msg)

            session.stop()
            self.session_handler.on_stop(session)

            idents = session.get_internal_attribute('identifiers_session_key')

            if not idents:
                idents = identifiers

            session_tuple = collections.namedtuple(
                'session_tuple', ['identifiers', 'session_key'])
            mysession = session_tuple(idents, session_key)

            self.session_event_handler.notify_stop(mysession)

        except InvalidSessionException:
            raise

        finally:
            # DG: this results in a redundant delete operation (from shiro).
            self.session_handler.after_stopped(session)

    # -------------------------------------------------------------------------
    # Session Creation Methods
    # -------------------------------------------------------------------------

    # consolidated with do_create_session:
    def _create_session(self, session_context):
        session = self.session_factory.create_session(session_context)

        msg = "Creating session. "
        logger.debug(msg)

        msg = ("Creating new EIS record for new session instance [{0}]".
               format(session))
        logger.debug(msg)

        sessionid = self.session_handler.create_session(session)
        if not sessionid:  # new to yosai
            msg = 'Failed to obtain a sessionid while creating session.'
            raise SessionCreationException(msg)

        return session

    # yosai.core.introduces the keyword parameterization
    def create_exposed_session(self, session, key=None, context=None):
        """
        :type session:  SimpleSession
        """
        # shiro ignores key and context parameters
        return DelegatingSession(self, DefaultSessionKey(session.session_id))

    # -------------------------------------------------------------------------
    # Session Lookup Methods
    # -------------------------------------------------------------------------

    # called by mgt.ApplicationSecurityManager:
    def get_session(self, key):
        """
        :returns: DelegatingSession
        """
        # a SimpleSession:
        session = self.session_handler.do_get_session(key)
        if (session):
            return self.create_exposed_session(session, key)
        else:
            return None

    # called internally:
    def _lookup_required_session(self, key):
        """
        :returns: SimpleSession
        """
        session = self.session_handler.do_get_session(key)
        if (not session):
            msg = ("Unable to locate required Session instance based "
                   "on session_key [" + str(key) + "].")
            raise UnknownSessionException(msg)
        return session

    # -------------------------------------------------------------------------
    # Session Attribute Methods
    # -------------------------------------------------------------------------

    # consolidated with check_valid
    def is_valid(self, session_key):
        """
        if the session doesn't exist, _lookup_required_session raises
        """
        try:
            self.check_valid(session_key)
            return True
        except InvalidSessionException:
            return False

    def check_valid(self, session_key):
        return self._lookup_required_session(session_key)

    def get_start_timestamp(self, session_key):
        return self._lookup_required_session(session_key).start_timestamp

    def get_last_access_time(self, session_key):
        return self._lookup_required_session(session_key).last_access_time

    def get_absolute_timeout(self, session_key):
        return self._lookup_required_session(session_key).absolute_timeout

    def get_idle_timeout(self, session_key):
        return self._lookup_required_session(session_key).idle_timeout

    def set_idle_timeout(self, session_key, idle_time):
        session = self._lookup_required_session(session_key)
        session.idle_timeout = idle_time
        self.session_handler.on_change(session)

    def set_absolute_timeout(self, session_key, absolute_time):
        session = self._lookup_required_session(session_key)
        session.absolute_timeout = absolute_time
        self.session_handler.on_change(session)

    def touch(self, session_key):
        session = self._lookup_required_session(session_key)
        session.touch()
        self.session_handler.on_change(session)

    def get_host(self, session_key):
        return self._lookup_required_session(session_key).host

    def get_internal_attribute_keys(self, session_key):
        session = self._lookup_required_session(session_key)
        collection = session.internal_attribute_keys
        try:
            return tuple(collection)
        except TypeError:  # collection is None
            return tuple()

    def get_internal_attribute(self, session_key, attribute_key):
        return self._lookup_required_session(session_key).\
            get_internal_attribute(attribute_key)

    def set_internal_attribute(self, session_key, attribute_key, value=None):
        if (value is None):
            self.remove_internal_attribute(session_key, attribute_key)
        else:
            session = self._lookup_required_session(session_key)
            session.set_internal_attribute(attribute_key, value)

            # if it's an internal attribute that is set, map the cached session
            # to the user id
            self.session_handler.on_change(session, update_identifiers_map=True)

    def remove_internal_attribute(self, session_key, attribute_key):
        # unless orphaned session/useridentifier map cache entries become an issue.. TBD
        session = self._lookup_required_session(session_key)
        removed = session.remove_internal_attribute(attribute_key)
        if (removed is not None):
            self.session_handler.on_change(session)
        return removed

    def get_attribute_keys(self, session_key):
        collection = self._lookup_required_session(session_key).attribute_keys
        try:
            return tuple(collection)
        except TypeError:  # collection is None
            return tuple()

    def get_attribute(self, session_key, attribute_key):
        return self._lookup_required_session(session_key).\
            get_attribute(attribute_key)

    def get_attributes(self, session_key, attribute_keys):
        """
        :type attribute_keys: a list of strings
        """
        return self._lookup_required_session(session_key).\
            get_attributes(attribute_keys)

    def set_attribute(self, session_key, attribute_key, value=None):
        if (value is None):
            self.remove_attribute(session_key, attribute_key)
        else:
            session = self._lookup_required_session(session_key)
            session.set_attribute(attribute_key, value)
            self.session_handler.on_change(session)

    # new to yosai
    def set_attributes(self, session_key, attributes):
        """
        :type attributes: dict
        """
        session = self._lookup_required_session(session_key)
        session.set_attributes(attributes)
        self.session_handler.on_change(session)

    def remove_attribute(self, session_key, attribute_key):
        session = self._lookup_required_session(session_key)
        removed = session.remove_attribute(attribute_key)
        if (removed is not None):
            self.session_handler.on_change(session)
        return removed

    def remove_attributes(self, session_key, attribute_keys):
        """
        :type attribute_keys: a list of strings
        """
        session = self._lookup_required_session(session_key)
        removed = session.remove_attributes(attribute_keys)
        if removed:
            self.session_handler.on_change(session)
        return removed


class DefaultSessionContext(MapContext, session_abcs.SessionContext):
    """
    This implementation refactors shiro's version quite a bit:
        - Accessor/mutator methods are omitted
            - getTypedValue isn't pythonic
            - attribute access is much more straightforward in python
        - key names aren't following the CLASSNAME.KEY convention
    """
    def __init__(self, context_map={}):
        """
        :type context_map: dict
        """
        super().__init__(context_map)

    # properties are used to enforce the interface:

    @property
    def host(self):
        return self.get('host')

    @host.setter
    def host(self, host):
        self.put('host', host)

    @property
    def session_id(self):
        return self.get('session_id')

    @session_id.setter
    def session_id(self, sessionid):
        # cannot set a session_id == None
        self.none_safe_put('session_id', sessionid)


class DefaultSessionStorageEvaluator(session_abcs.SessionStorageEvaluator):

    # Global policy determining whether Subject sessions may be used to persist
    # Subject state if the Subject's Session does not yet exist.

    def __init__(self):
        self._session_storage_enabled = True

    @property
    def session_storage_enabled(self):
        return self._session_storage_enabled

    @session_storage_enabled.setter
    def session_storage_enabled(self, sse):
        self._session_storage_enabled = sse

    def is_session_storage_enabled(self, subject=None):
        if (not subject):
            return self._session_storage_enabled
        else:
            return ((subject is not None and
                     subject.get_session(False) is not None) or
                    bool(self._session_storage_enabled))
