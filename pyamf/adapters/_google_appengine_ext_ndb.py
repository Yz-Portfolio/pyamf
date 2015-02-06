# Copyright (c) The PyAMF Project.
# See LICENSE.txt for details.

"""
Google App Engine ndb adapter module.
"""

import collections
import datetime

from google.appengine.ext import ndb
from google.appengine.ext.ndb import polymodel

import pyamf
from pyamf.adapters import util, models as adapter_models


class NdbModelStub(object):
    """
    This class represents a C{ndb.Model} or C{ndb.Expando} class as the typed
    object is being read from the AMF stream. Once the attributes have been
    read from the stream and through the magic of Python, the instance of this
    class will be converted into the correct type.
    """


class GAEReferenceCollection(dict):
    """
    This helper class holds a dict of klass to key/objects loaded from the
    Datastore.

    @since: 0.4.1
    """

    def _getClass(self, klass):
        if not issubclass(klass, (ndb.Model, ndb.Expando)):
            raise TypeError('expected ndb.Model/ndb.Expando class, got %s' % (
                klass,
            ))

        return self.setdefault(klass, {})

    def getClassKey(self, klass, key):
        """
        Return an instance based on klass/key.

        If an instance cannot be found then C{KeyError} is raised.

        @param klass: The class of the instance.
        @param key: The key of the instance.
        @return: The instance linked to the C{klass}/C{key}.
        @rtype: Instance of L{klass}.
        """
        d = self._getClass(klass)

        return d[key]

    def addClassKey(self, klass, key, obj):
        """
        Adds an object to the collection, based on klass and key.

        @param klass: The class of the object.
        @param key: The datastore key of the object.
        @param obj: The loaded instance from the datastore.
        """
        d = self._getClass(klass)

        d[key] = obj


class StubCollection(object):
    """
    A mapping of `NdbModelStub` instances to key/id. As the AMF graph is
    decoded, L{NdbModelStub} instances are created as markers to be replaced
    in the finalise stage of decoding. At that point all the ndb entities are
    fetched from the datastore and hydrated in to proper Python objects and
    the stubs are transformed in to this objects so that referential integrity
    is maintained.

    A complete hack because of the flexibility of Python but it works ..

    @ivar stubs:
    """

    def __init__(self):
        self.stubs = collections.OrderedDict()
        self.to_fetch = []
        self.fetched_entities = None

    def addStub(self, stub, alias, attrs, key):
        """
        Add a stub to this collection.

        @param stub: The L{NdbModelStub} instance.
        @param alias: The L{pyamf.ClassAlias} linked to this stub.
        @param attrs: The decoded name -> value mapping of attributes.
        @param key: The ndb key string if known.
        """
        if stub not in self.stubs:
            self.stubs[stub] = (alias.klass, attrs, key)

        if key:
            self.to_fetch.append(key)

    def transformStub(self, stub, klass, attrs, key):
        stub.__dict__.clear()
        stub.__class__ = klass

        for k, v in attrs.items():
            if not isinstance(v, NdbModelStub):
                continue

            self.transform(v)

        if key is None:
            stub.__init__(**attrs)

            return

        ds_entity = self.fetched_entities.get(key, None)

        if not ds_entity:
            attrs['key'] = key
            stub.__init__(**attrs)
        else:
            stub.__dict__.update(ds_entity.__dict__)

            for k, v in attrs.items():
                setattr(stub, k, v)

    def fetchEntities(self):
        return dict(zip(self.to_fetch, ndb.get_multi(self.to_fetch)))

    def transform(self, stub=None):
        if self.fetched_entities is None:
            self.fetched_entities = self.fetchEntities()

        if stub is not None:
            stub, klass, attrs, key = self.stubs.pop(self.stubs.index(stub))

            self.transformStub(stub, klass, attrs, key)

            return

        for stub, (klass, attrs, key) in self.stubs.iteritems():
            self.transformStub(stub, klass, attrs, key)


class NdbClassAlias(pyamf.ClassAlias):
    """
    This class contains all the business logic to interact with Google's
    Datastore API's. Any C{ndb.Model} or C{ndb.Expando} classes will use this
    class alias for encoding/decoding.

    We also add a number of indexes to the encoder context to aggressively
    decrease the number of Datastore API's that we need to complete.
    """

    # The name of the attribute used to represent the key
    KEY_ATTR = '_key'

    def _compile_base_class(self, klass):
        if klass in (ndb.Model, polymodel.PolyModel):
            return

        pyamf.ClassAlias._compile_base_class(self, klass)

    def _finalise_compile(self):
        pyamf.ClassAlias._finalise_compile(self)

        self.shortcut_decode = False

    def createInstance(self, codec=None):
        return NdbModelStub()

    def makeStubCollection(self):
        return StubCollection()

    def getStubCollection(self, codec):
        extra = codec.context.extra

        stubs = extra.get('gae_ndb_entities', None)

        if not stubs:
            stubs = extra['gae_ndb_entities'] = self.makeStubCollection()

        return stubs

    def getCustomProperties(self):
        props = {}
        # list of property names that are considered read only
        read_only_props = []
        repeated_props = {}
        # list of property names that are computed
        computed_props = {}

        for name, prop in self.klass._properties.iteritems():
            props[name] = prop

            if prop._repeated:
                repeated_props[name] = prop

            if isinstance(prop, ndb.ComputedProperty):
                computed_props[name] = prop

        if issubclass(self.klass, polymodel.PolyModel):
            del props['class']

        # check if the property is a defined as a computed property. These
        # types of properties are read-only and the datastore freaks out if
        # you attempt to meddle with it. We delete the attribute entirely ..
        for name, value in self.klass.__dict__.iteritems():
            if isinstance(value, ndb.ComputedProperty):
                read_only_props.append(name)

        self.encodable_properties.update(props.keys())
        self.decodable_properties.update(props.keys())
        self.readonly_attrs.update(read_only_props)

        if computed_props:
            self.decodable_properties.difference_update(computed_props.keys())

        self.model_properties = props or None
        self.repeated_properties = repeated_props or None
        self.computed_properties = computed_props or None

    def getDecodableAttributes(self, obj, attrs, codec=None):
        attrs = pyamf.ClassAlias.getDecodableAttributes(
            self, obj, attrs, codec=codec
        )

        key = attrs.pop(self.KEY_ATTR, None)

        if key:
            key = ndb.Key(urlsafe=key)

        if self.model_properties:
            property_attrs = [
                k for k in attrs if k in self.decodable_properties
            ]

            for name in property_attrs:
                prop = self.model_properties.get(name, None)

                if not prop:
                    continue

                value = attrs[name]

                if not prop._repeated:
                    attrs[name] = adapter_models.decode_model_property(
                        prop,
                        value
                    )

                    continue

                if not value:
                    attrs[name] = []

                    continue

                for idx, val in enumerate(value):
                    value[idx] = adapter_models.decode_model_property(
                        prop,
                        val
                    )

                attrs[name] = value

        stubs = self.getStubCollection(codec)

        stubs.addStub(obj, self, attrs, key)

        return attrs

    def getEncodableAttributes(self, obj, codec=None):
        attrs = pyamf.ClassAlias.getEncodableAttributes(
            self, obj, codec=codec
        )

        for k in attrs.keys()[:]:
            if k.startswith('_'):
                del attrs[k]

        if self.model_properties:
            for name in self.encodable_properties:
                prop = self.model_properties.get(name, None)

                if not prop:
                    continue

                attrs[name] = self.getAttribute(obj, name, codec=codec)

                if prop._repeated:
                    prop_value = attrs[name]

                    for idx, value in enumerate(prop_value):
                        prop_value[idx] = adapter_models.encode_model_property(
                            prop,
                            value,
                        )

                    continue

                attrs[name] = adapter_models.encode_model_property(
                    prop,
                    attrs[name],
                )

        attrs[self.KEY_ATTR] = unicode(obj.key.urlsafe()) if obj.key else None

        return attrs


def get_ndb_context(context):
    """
    Returns a reference to the C{gae_ndb_objects} on the context. If it doesn't
    exist then it is created.

    @param context: The context to load the C{gae_ndb_objects} index from.
    @return: The C{gae_ndb_objects} index reference.
    @rtype: Instance of L{GAEReferenceCollection}
    @since: 0.4.1
    """
    try:
        return context['gae_ndb_context']
    except KeyError:
        r = context['gae_ndb_context'] = GAEReferenceCollection()

        return r


def encode_ndb_instance(obj, encoder=None):
    """
    The GAE Datastore creates new instances of objects for each get request.
    This is a problem for PyAMF as it uses the id(obj) of the object to do
    reference checking.

    We could just ignore the problem, but the objects are conceptually the
    same so the effort should be made to attempt to resolve references for a
    given object graph.

    We create a new map on the encoder context object which contains a dict of
    C{object.__class__: {key1: object1, key2: object2, .., keyn: objectn}}. We
    use the datastore key to do the reference checking.

    @since: 0.4.1
    """
    if not obj.key or not obj.key.id():
        encoder.writeObject(obj)

        return

    referenced_object = _get_by_class_key(
        encoder,
        obj.__class__,
        obj.key,
        obj
    )

    encoder.writeObject(referenced_object)


def encode_ndb_key(key, encoder=None):
    """
    When encountering an L{ndb.Key} instance, find the entity in the datastore
    and encode that.
    """
    klass = ndb.Model._kind_map.get(key.kind())

    referenced_object = _get_by_class_key(
        encoder,
        klass,
        key,
    )

    if not referenced_object:
        encoder.writeNull(None)

        return

    encoder.writeObject(referenced_object)


def _get_by_class_key(codec, klass, key, obj=None):
    gae_objects = get_ndb_context(codec.context.extra)

    try:
        return gae_objects.getClassKey(klass, key)
    except KeyError:
        if not obj:
            obj = key.get()

        gae_objects.addClassKey(klass, key, obj)

        return obj


@adapter_models.register_property_decoder(ndb.KeyProperty)
def decode_key_property(prop, value):
    if not value:
        return None

    return ndb.Key(urlsafe=value)


@adapter_models.register_property_decoder(ndb.DateProperty)
def decode_time_property(prop, value):
    if not hasattr(value, 'date'):
        return value

    return value.date()


@adapter_models.register_property_decoder(ndb.FloatProperty)
def decode_float_property(prop, value):
    if isinstance(value, (int, long)):
        return float(value)

    return value


@adapter_models.register_property_decoder(ndb.IntegerProperty)
def decode_int_property(prop, value):
    if isinstance(value, float):
        long_val = long(value)

        # only convert the type if there is no mantissa - otherwise
        # let the chips fall where they may
        if long_val == value:
            return long_val

    return value


@adapter_models.register_property_encoder(ndb.KeyProperty)
def encode_key_property(prop, value):
    if not hasattr(value, 'urlsafe'):
        return value

    return value.urlsafe()


@adapter_models.register_property_encoder(ndb.TimeProperty)
def encode_time_property(prop, value):
    # PyAMF supports datetime.datetime objects and won't decide what date to
    # add to this time value. Users will have to figure it out themselves
    raise pyamf.EncodeError('ndb.TimeProperty is not supported by PyAMF')


@adapter_models.register_property_encoder(ndb.DateProperty)
def encode_date_property(prop, value):
    if not value:
        return value

    return datetime.datetime.combine(
        value,
        datetime.time(0, 0, 0)
    )


def post_ndb_process(payload, context):
    """
    """
    stubs = context.get('gae_ndb_entities', None)

    if not stubs:
        return payload

    stubs.transform()

    return payload


# small optimisation to compile the ndb.Model base class
if hasattr(ndb.model, '_NotEqualMixin'):
    not_equal_mixin = pyamf.register_class(ndb.model._NotEqualMixin)
    not_equal_mixin.compile()

    del not_equal_mixin

# initialise the module here: hook into pyamf
pyamf.register_alias_type(NdbClassAlias, ndb.Model, ndb.Expando)
pyamf.add_type(ndb.Query, util.to_list)
pyamf.add_type(ndb.Model, encode_ndb_instance)
pyamf.add_post_decode_processor(post_ndb_process)
pyamf.add_type(ndb.Key, encode_ndb_key)
