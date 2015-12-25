#
# Copyright (c) 2013, Digium, Inc.
#

"""Swagger client library.
"""

import json
import os.path
import re
import urllib
import urlparse
import swaggerpy

from tornado.log import app_log as log
from tornado.ioloop import IOLoop
from tornado.gen import coroutine, Return
from tornado.httpclient import AsyncHTTPClient
from tornado.websocket import websocket_connect
from swaggerpy.processors import WebsocketProcessor, SwaggerProcessor


class ClientProcessor(SwaggerProcessor):
    """Enriches swagger models for client processing.
    """

    def process_resource_listing_api(self, resources, listing_api, context):
        """Add name to listing_api.

        :param resources: Resource listing object
        :param listing_api: ResourceApi object.
        :type context: ParsingContext
        :param context: Current context in the API.
        """
        name, ext = os.path.splitext(os.path.basename(listing_api['path']))
        listing_api['name'] = name


class Operation(object):
    """Operation object.
    """

    def __init__(self, uri, operation, http_client):
        """

        :param uri:
        :param operation:
        :param http_client: HTTP client
        :type http_client: AsyncHTTPClient
        :return:
        """
        self.uri = uri
        self.json = operation
        self.http_client = http_client

    def __repr__(self):
        return "%s(%s)" % (self.__class__.__name__, self.json['nickname'])

    @coroutine
    def __call__(self, ws_on_message=None, **kwargs):
        """Invoke ARI operation.

        :param kwargs: ARI operation arguments.
        :return: Implementation specific response or WebSocket connection
        """
        log.info("%s?%r" % (self.json['nickname'], urllib.urlencode(kwargs)))
        method = self.json['httpMethod']
        uri = self.uri
        params = {}
        data = None
        headers = None
        for param in self.json.get('parameters', []):
            pname = param['name']
            value = kwargs.get(pname)
            # Turn list params into comma separated values
            if isinstance(value, list):
                value = ",".join(value)

            if value is not None:
                if param['paramType'] == 'path':
                    uri = uri.replace('{%s}' % pname,
                                      urllib.quote_plus(str(value)))
                elif param['paramType'] == 'query':
                    params[pname] = value
                elif param['paramType'] == 'body':
                    if isinstance(value, dict):
                        if data:
                            data.update(value)
                        else:
                            data = value
                    else:
                        raise TypeError(
                            "Parameters of type 'body' require dict input")
                else:
                    raise AssertionError(
                        "Unsupported paramType %s" %
                        param['paramType'])
                del kwargs[pname]
            else:
                if param['required']:
                    raise TypeError(
                        "Missing required parameter '%s' for '%s'" %
                        (pname, self.json['nickname']))
        if kwargs:
            raise TypeError("'%s' does not have parameters %r" %
                            (self.json['nickname'], kwargs.keys()))

        log.info("%s %s(%r)", method, uri, params)

        if data:
            data = json.dumps(data)
            headers = {'Content-type': 'application/json',
                       'Accept': 'application/json'}

        if self.json['is_websocket']:
            # Fix up http: URLs
            uri = re.sub('^http', "ws", uri)
            if data:
                raise NotImplementedError(
                    "Sending body data with websockets not implmented")
            ws = yield websocket_connect(
                urlparse.urljoin(uri, urllib.urlencode(params)),
                on_message_callback=ws_on_message
            )
            raise Return(ws)
        else:
            result = yield self.http_client.fetch(
                urlparse.urljoin(uri, urllib.urlencode(params)),
                method=method,
                body=data,
                headers=headers
            )
            raise Return(result)


class Resource(object):
    """Swagger resource, described in an API declaration.

    :param resource: Resource model
    :param http_client: HTTP client API
    """

    def __init__(self, resource, http_client):
        log.debug("Building resource '%s'" % resource['name'])
        self.json = resource
        decl = resource['api_declaration']
        self.http_client = http_client
        self.operations = {
            oper['nickname']: self._build_operation(decl, api, oper)
            for api in decl['apis']
            for oper in api['operations']}

    def __repr__(self):
        return "%s(%s)" % (self.__class__.__name__, self.json['name'])

    def __getattr__(self, item):
        """Promote operations to be object fields.

        :param item: Name of the attribute to get.
        :rtype: Resource
        :return: Resource object.
        """
        op = self.get_operation(item)
        if not op:
            raise AttributeError("Resource '%s' has no operation '%s'" %
                                 (self.get_name(), item))
        return op

    def get_operation(self, name):
        """Gets the operation with the given nickname.

        :param name: Nickname of the operation.
        :rtype:  Operation
        :return: Operation, or None if not found.
        """
        return self.operations.get(name)

    def get_name(self):
        """Returns the name of this resource.

        Name is derived from the filename of the API declaration.

        :return: Resource name.
        """
        return self.json.get('name')

    def _build_operation(self, decl, api, operation):
        """Build an operation object

        :param decl: API declaration.
        :param api: API entry.
        :param operation: Operation.
        """
        log.debug("Building operation %s.%s" % (
            self.get_name(), operation['nickname']))
        uri = decl['basePath'] + api['path']
        return Operation(uri, operation, self.http_client)


class SwaggerClient(object):
    """Client object for accessing a Swagger-documented RESTful service.

    :param http_client: HTTP client API
    :type  http_client: HttpClient
    """
    _api_docs = None
    _resources = None

    @property
    def api_docs(self):
        if self._api_docs is None:
            raise RuntimeError('Not loaded')
        return self._api_docs

    @api_docs.setter
    def api_docs(self, value):
        self._api_docs = value

    @property
    def resources(self):
        if self._resources is None:
            raise RuntimeError('Not loaded')
        return self._resources

    @resources.setter
    def resources(self, value):
        self._resources = value

    def __init__(self, url_or_resource, io_loop=None, http_client=None,
                 on_load=None, **kwargs):
        if io_loop is None:
            io_loop = IOLoop.current()
        self.io_loop = io_loop
        if not http_client:
            http_client = AsyncHTTPClient(defaults=kwargs)
        self.http_client = http_client
        future = self.load(url_or_resource)
        if future is not None:
            def callback(f):
                f.result()
                if on_load is not None:
                    on_load()
            io_loop.add_future(future, callback)
        elif on_load is not None:
            on_load()

    @coroutine
    def load(self, url_or_resource):
        """
        :param url_or_resource: Either the parsed resource listing+API decls,
                                or its URL.
        :type url_or_resource: dict or str
        """
        loader = swaggerpy.Loader(
            self.http_client, [WebsocketProcessor(), ClientProcessor()])

        if isinstance(url_or_resource, str):
            log.debug("Loading from %s" % url_or_resource)
            self.api_docs = yield loader.load_resource_listing(url_or_resource)
        else:
            log.debug("Loading from %s" % url_or_resource.get('basePath'))
            self.api_docs = url_or_resource
            loader.process_resource_listing(self.api_docs)

        self.resources = {
            resource['name']: Resource(resource, self.http_client)
            for resource in self.api_docs['apis']}

    def __repr__(self):
        return "%s(%s)" % (self.__class__.__name__, self.api_docs['basePath'])

    def __getattr__(self, item):
        """Promote resource objects to be client fields.

        :param item: Name of the attribute to get.
        :return: Resource object.
        """
        resource = self.get_resource(item)
        if not resource:
            raise AttributeError("API has no resource '%s'" % item)
        return resource

    def close(self):
        """Close the SwaggerClient, and underlying resources.
        """
        self.http_client.close()

    def get_resource(self, name):
        """Gets a Swagger resource by name.

        :param name: Name of the resource to get
        :rtype: Resource
        :return: Resource, or None if not found.
        """
        return self.resources.get(name)
