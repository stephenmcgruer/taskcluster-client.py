"""This module is used to interact with taskcluster rest apis"""

import os
import json
import logging
import copy
try:
  import urlparse
except ImportError:
  import urllib.parse as urlparse
import time


# For finding apis.json
from pkg_resources import resource_string
import requests
import hawk

from . import exceptions

log = logging.getLogger(__name__)
log.setLevel(logging.DEBUG)
# Only for debugging XXX: turn off the True or thing when shipping
if os.environ.get('DEBUG_TASKCLUSTER_CLIENT'):
  log.addHandler(logging.StreamHandler())
log.addHandler(logging.NullHandler())

API_CONFIG = json.loads(resource_string(__name__, 'apis.json').decode('utf-8'))

# Default configuration
_defaultConfig = config = {
  'credentials': {
    'clientId': os.environ.get('TASKCLUSTER_CLIENT_ID'),
    'accessToken': os.environ.get('TASKCLUSTER_ACCESS_TOKEN'),
    'certificate': os.environ.get('TASKCLUSTER_CERTIFICATE'),
    'algorithm': 'sha256',
  },
  # This dict is in the JS client but doesn't seem to be consumed...
  # Should we get rid of it in both?
  'authorization': {
    'delegating': False,
    'scopes': [],
  },
  'maxRetries': 5,
  'signedUrlExpiration': 15 * 60,
}


class BaseClient(object):
  """ Instances of this class are API helpers for a specific API reference.
  They know how to load their data from either a statically defined JSON file
  which is packaged with the library, or by reading an environment variable can
  load the individual api endpoints from the web

  A difference between the JS and Python client is that the payload for JS
  client is always the last argument in the API method.  That works nicely
  because JS doesn't have keyword arguments.  Python does have kwargs and I'd
  like to support them (because I like to use them). The result is that I'm
  going to make the payload a mandatory keyword argument for methods which have
  an input schema.

  Failing API calls which are connection or server related (i.e. 500 series
  errors) will be retried up to a maximum number of times with an exponential
  backoff.  This functionality mirrors that of the JS Client library.  Success
  and errors which aren't connection related are returned on the first
  iteration.
  """

  def __init__(self, options=None):
    if not options:
      options = {}
    # Remember, ._options is defined in the type() call below
    self._options.update(options)

  @property
  def options(self):
    """Hold all the API Level options.  This method returns
    a dictionary that's a collation of the API level options
    and the module level defaults.  It will return a new 
    dictionary every time it's called"""
    options = {}
    options.update(_defaultConfig)
    options.update(self._options)
    return options

  def setOption(self, key, value):
    """ Set an API level option.  Options specified here will
    override those specified in the module level .config dict"""

    self._options[key] = value

  def makeHawkExt(self):
    """ Make an 'ext' for Hawk authentication """
    o = self.options
    c = o.get('credentials', {})
    if c.get('clientId') and c.get('accessToken'):
      ext = {}
      cert = c.get('certificate')
      if cert:
        if isinstance(cert, basestring):
          cert = json.loads(cert)
        ext['certificate'] = cert
    
      if 'authorizedScopes' in o:
        ext['authorizedScopes'] = o['authorizedScopes']

      # .encode('base64') inserts a newline, which hawk doesn't
      # like but doesn't strip itself
      return json.dumps(ext).encode('base64').strip()
    else:
      return None

  def _makeTopicExchange(self, entry, *args, **kwargs):
    if len(args) == 0 and not kwargs:
      routingKeyPattern = {}
    elif len(args) >= 1:
      if kwargs or len(args) != 1:
        errStr = 'Pass either a string, single dictionary or only kwargs'
        raise exceptions.TaskclusterTopicExchangeFailure(errStr)
      routingKeyPattern = args[0]
    else:
      routingKeyPattern = kwargs

    data = {
      'exchange': '%s/%s' % (self.options['exchangePrefix'].rstrip('/'),
                             entry['exchange'].lstrip('/'))
    }

    # If we are passed in a string, we can short-circuit this function
    if isinstance(routingKeyPattern, basestring):
      log.debug('Passing through string for topic exchange key')
      data['routingKeyPattern'] = routingKeyPattern
      return data

    if type(routingKeyPattern) != dict:
      errStr = 'routingKeyPattern must eventually be a dict'
      raise exceptions.TaskclusterTopicExchangeFailure(errStr)

    if not routingKeyPattern:
      routingKeyPattern = {}

    # There is no canonical meaning for the maxSize and required
    # reference entry in the JS client, so we don't try to define
    # them here, even though they sound pretty obvious
    
    routingKey = []
    for key in entry['routingKey']:
      if 'constant' in key:
        value = key['constant']
      elif key['name'] in routingKeyPattern:
        log.debug('Found %s in routing key params', key['name'])
        value = str(routingKeyPattern[key['name']])
        if not key.get('multipleWords') and '.' in value:
          raise exceptions.TaskclusterTopicExchangeFailure('Cannot have periods in single word keys')
      else:
        value = '#' if key.get('multipleWords') else '*'
        log.debug('Did not find %s in input params, using %s', key['name'], value)
      
      routingKey.append(value)

    data['routingKeyPattern'] = '.'.join([str(x) for x in routingKey])
    return data

  def buildUrl(self, methodName, *args, **kwargs):
    entry = None
    for x in self._api['entries']:
      if x['name'] == methodName:
        entry = x
    if not entry:
      raise exceptions.TaskclusterFailure('Requested method "%s" not found in API Reference' % methodName)
    apiArgs = self._processArgs(entry['args'], *args, **kwargs)
    route = self._subArgsInRoute(entry['route'], apiArgs)
    return self.options['baseUrl'] + '/' + route

  def buildSignedUrl(self, methodName, *args, **kwargs):
    if 'expiration' in kwargs:
      expiration = kwargs['expiration']
      del kwargs['expiration']
    else:
      expiration = self.options['signedUrlExpiration']
    
    expiration = int(expiration) # Mainly so that we throw if it's not a number

    requestUrl = self.buildUrl(methodName, *args, **kwargs)

    opts = self.options
    cred = opts.get('credentials')
    if not cred or not 'clientId' in cred or not 'accessToken' in cred:
      raise exceptions.TaskclusterFailure('Invalid Hawk Credentials') 
    
    clientId = self.options['credentials']['clientId']
    accessToken = self.options['credentials']['accessToken']

    bewitOpts = {
      'credentials': {
        'id': clientId,
        'key': accessToken,
        'algorithm': 'sha256',
      },
      'ttl_sec': expiration,
      'ext': self.makeHawkExt(),
    }

    # NOTE: the version of PyHawk in pypi is broken.
    # see: https://github.com/mozilla/PyHawk/pull/27
    bewit = hawk.client.get_bewit(requestUrl, bewitOpts)
    if not bewit:
      raise exceptions.TaskclusterFailure('Did not receive a bewit')

    u = urlparse.urlparse(requestUrl) 
    #urlParts.query += 'bewit=%s' % bewit
    return urlparse.urlunparse((
      u.scheme,
      u.netloc,
      u.path,
      u.params,
      u.query + 'bewit=%s' % bewit,
      u.fragment,
    ))


  def _makeApiCall(self, entry, *args, **kwargs):
    """ This function is used to dispatch calls to other functions
    for a given API Reference entry"""

    payload = None

    # I forget if **ing a {} results in a new {} or a reference
    _kwargs = copy.deepcopy(kwargs)
    if 'input' in entry and 'payload' in _kwargs:
      payload = _kwargs['payload']
      del _kwargs['payload']
      log.debug('We have a payload!')

    apiArgs = self._processArgs(entry['args'], *args, **_kwargs)
    route = self._subArgsInRoute(entry['route'], apiArgs)
    log.debug('Route is: %s', route)

    return self._makeHttpRequest(entry['method'], route, payload)

  def _processArgs(self, reqArgs, *args, **kwargs):
    """ Take the list of required arguments, positional arguments
    and keyword arguments and return a dictionary which maps the
    value of the given arguments to the required parameters.

    Keyword arguments will overwrite positional arguments.
    """

    data = {}

    # We know for sure that if we don't give enough arguments that the call
    # should fail.  We don't yet know if we should fail because of two many
    # arguments because we might be overwriting positional ones with kw ones
    if len(reqArgs) > len(args) + len(kwargs):
      raise exceptions.TaskclusterFailure('API Method was not given enough args')

    # We also need to error out when we have more positional args than required
    # because we'll need to go through the lists of provided and required args
    # at the same time.  Not disqualifying early means we'll get IndexErrors if
    # there are more positional arguments than required
    if len(args) > len(reqArgs):
      raise exceptions.TaskclusterFailure('API Method was called with too many positional args')

    i = 0
    for arg in args:
      log.debug('Found a positional argument: %s', arg)
      data[reqArgs[i]] = arg
      i += 1

    log.debug('After processing positional arguments, we have: %s', data)

    data.update(kwargs)

    log.debug('After keyword arguments, we have: %s', data)

    if len(reqArgs) != len(data):
      errMsg = 'API Method takes %d args, %d given' % (len(reqArgs), len(data))
      log.error(errMsg)
      raise exceptions.TaskclusterFailure(errMsg)

    for reqArg in reqArgs:
      if reqArg not in data:
        errMsg = 'API Method requires a "%s" argument' % reqArg
        log.error(errMsg)
        raise exceptions.TaskclusterFailure(errMsg)

    return data

  def _subArgsInRoute(self, route, args):
    """ Given a route like "/task/<taskId>/artifacts" and a mapping like
    {"taskId": "12345"}, return a string like "/task/12345/artifacts"
    """

    if route.count('<') != route.count('>'):
      raise exceptions.TaskclusterFailure('Mismatched arguments in route')

    if route.count('<') != len(args):
      raise exceptions.TaskclusterFailure('Incorrect number of arguments for route')

    # TODO: Let's just pretend this is a good idea
    try:
      route = route.replace('<', '%(').replace('>', ')s') % args
    except KeyError:
      raise exceptions.TaskclusterFailure('Argument not found in route')
    return route.lstrip('/')

  def _makeHttpRequest(self, method, route, payload):
    """ Make an HTTP Request for the API endpoint.  This method wraps
    the logic about doing failure retry and passes off the actual work
    of doing an HTTP request to another method."""

    baseUrl = self.options['baseUrl']
    # urljoin ignores the last param of the baseUrl if the base url doesn't end
    # in /.  I wonder if it's better to just do something basic like baseUrl +
    # route instead
    if not baseUrl.endswith('/'):
      baseUrl += '/'
    fullUrl = urlparse.urljoin(baseUrl, route.lstrip('/'))
    log.debug('Full URL used is: %s', fullUrl)

    retry = 0
    response = None
    while retry < self.options['maxRetries']:
      log.debug('Making attempt %d', retry)
      response = self._makeSingleHttpRequest(method, fullUrl, payload)

      # We only want to consider connection errors for retry.  Other errors,
      # like a 404 saying that a resource wasn't found should be returned
      # immediately
      if response.status_code >= 500 and response.status_code < 600:
        log.error('Received HTTP Status %d, retrying', response.status_code)
        retry += 1
        snooze = float(retry * retry) / 10.0
        log.info('Sleeping %0.2f seconds for exponential backoff', snooze)
        time.sleep(snooze)
      else:
        break

    # We want to send JSON data back to the caller
    try:
      # We want to make sure that calling code handles errors
      response.raise_for_status()
      apiData = response.json()
    except requests.exceptions.RequestException as rerr:
      errStr = 'Request failed to complete'
      log.error(errStr)
      raise exceptions.TaskclusterRestFailure(errStr, superExc=rerr)
    except ValueError as ve:
      errStr = 'Response contained malformed JSON data'
      log.error(errStr)
      raise exceptions.TaskclusterRestFailure(errStr, superExc=ve, res=response)

    return apiData

  def _makeSingleHttpRequest(self, method, url, payload):
    log.debug('Making a %s request to %s', method, url)
    opts = self.options
    cred = opts.get('credentials')
    if cred and 'clientId' in cred and 'accessToken' in cred:
      cert = cred.get('certificate')

      if cert and isinstance(cert, basestring):
        cert = json.loads(cert)
         
      hawkOpts = {
        'credentials': {
          'id': cred['clientId'],
          'key': cred['accessToken'],
          'algorithm': cred.get('algorithm', 'sha256'),
        },
        'ext': self.makeHawkExt(),
      }
      header = hawk.client.header(url, method, hawkOpts)
      headers = {'Authorization': header['field']}
    else:
      log.info('Not using hawk!')
      headers = {}

    return requests.request(method, url, data=payload, headers=headers)


def createApiClient(name, api):
  attributes = dict(
    name=name,
    _api=api['reference'],
    __doc__=api.get('description'),
    _options={}, 
  )

  for opt in filter(lambda x: x != 'entries', api['reference']):
    attributes['_options'][opt] = api['reference'][opt]

  for entry in api['reference']['entries']:
    if entry['type'] == 'function':

      def addApiCall(e):
        def apiCall(self, *args, **kwargs):
          return self._makeApiCall(e, *args, **kwargs)
        return apiCall

      f = addApiCall(entry)
      docStr = "Call the %s api's %s method.  " % (name, entry['name'])

      if entry['args'] and len(entry['args']) > 0:
        docStr += "This method takes:\n\n"
        docStr += '\n'.join(['- ``%s``' % x for x in entry['args']])
        docStr += '\n\n'
      else:
        docStr += "This method takes no arguments.  "

      if 'input' in entry:
        docStr += "This method takes input ``%s``.  " % entry['input']

      if 'output' in entry:
        docStr += "This method gives output ``%s``" % entry['output']

      docStr += '\n\nThis method does a ``%s`` to ``%s``.' % (entry['method'].upper(), entry['route'])

      f.__doc__ = docStr

    elif entry['type'] == 'topic-exchange':
      def addTopicExchange(e):
        def topicExchange(self, *args, **kwargs):
          return self._makeTopicExchange(e, *args, **kwargs)
        return topicExchange

      f = addTopicExchange(entry)

      docStr = 'Generate a routing key pattern for the %s exchange.  ' % entry['exchange']
      docStr += 'This method takes a given routing key as a string or a '
      docStr += 'dictionary.  For each given dictionary key, the corresponding '
      docStr += 'routing key token takes its value.  For routing key tokens '
      docStr += 'which are not specified by the dictionary, the * or # character '
      docStr += 'is used depending on whether or not the key allows multiple words.\n\n'
      docStr += 'This exchange takes the following keys:\n\n'
      docStr += '\n'.join(['- ``%s``' % x['name'] for x in entry['routingKey']])

      f.__doc__ = docStr
  
    # Give the function the right name
    f.__name__ = str(entry['name'])
    
    # Add whichever function we created
    attributes[entry['name']] = f



  return type(name.encode('utf-8'), (BaseClient,), attributes)


# This has to be done after the Client class is declared
for key, value in API_CONFIG.items():
  globals()[key] = createApiClient(key, value)
