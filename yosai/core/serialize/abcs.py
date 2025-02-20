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

from abc import ABCMeta, abstractmethod
from yosai.core import DeserializationException


class Serializable(metaclass=ABCMeta):

    @classmethod
    @abstractmethod
    def serialization_schema(cls):
        """
        Each serializable class must define its respective Schema (marshmallow)
        and its @post_load 'make_object' method.

        :returns: a SerializationSchema class
        """
        pass

    def serialize(self):
        """
        :returns: a dict
        """
        schema = self.serialization_schema()()
        return schema.dump(self).data

    @classmethod
    def deserialize(cls, data):
        """
        :returns: a deserialized object
        """
        schema = cls.serialization_schema()()
        result = schema.load(data=data)
        if result.errors:
            msg = 'Failed to de-serialize:  data={0}, errors={1}'.\
                  format(result.data, result.errors)
            raise DeserializationException(msg)
        return result.data

    def __eq__(self, other):
        if self is other:
            return True

        return (isinstance(other, self.__class__) and
                self.__dict__ == other.__dict__)


class Serializer(metaclass=ABCMeta):

    @classmethod
    @abstractmethod
    def serialize(self, obj):
        pass

    @classmethod
    @abstractmethod
    def deserialize(self, message):
        pass
