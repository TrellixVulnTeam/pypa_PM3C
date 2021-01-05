# import defusedxml before anything else
import defusedxml
import defusedxml.xmlrpc
defusedxml.xmlrpc.monkey_patch()

# system imports
import sys, os, urllib, cStringIO, traceback, cgi, binascii, functools
import time, smtplib, base64, types, urlparse
import re, Cookie, subprocess, hashlib
import logging
from zope.pagetemplate.pagetemplatefile import PageTemplateFile
from distutils.util import rfc822_escape
from xml.etree import cElementTree
import itsdangerous
import redis
import rq
import boto.s3

from rfc3986 import uri_reference

try:
    import json
except ImportError:
    import simplejson as json
try:
    import psycopg2
    OperationalError = psycopg2.OperationalError
    IntegrityError = psycopg2.IntegrityError
except ImportError:
    class OperationalError(Exception):
        pass

# Raven for error reporting
import raven
import raven.utils.wsgi
from raven.handlers.logging import SentryHandler

# Filesystem Handling
import fs.errors
import fs.multifs
import fs.osfs
import fs.s3fs

import readme_renderer.rst
import readme_renderer.txt

# local imports
import store, config, rpc
import MailingLogger, gae
from mini_pkg_resources import safe_name
import tasks

from perfmetrics import statsd_client
from perfmetrics import set_statsd_client

from dogadapter import dogstatsd

from constants import DOMAIN_BLACKLIST

# Authomatic
from authadapters import PyPIAdapter
import authomatic
from authomatic.providers import openid
from authomatic.providers import oauth2
from browserid.jwt import parse

root = os.path.dirname(os.path.abspath(__file__))
conf = config.Config(os.path.join(root, "config.ini"))

STATSD_URI = "statsd://127.0.0.1:8125?prefix=%s" % (conf.database_name)
set_statsd_client(STATSD_URI)


EMPTY_RSS = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE rss PUBLIC "-//Netscape Communications//DTD RSS 0.91//EN" "http://my.netscape.com/publish/formats/rss-0.91.dtd">
<rss version="0.91">
 <channel>
  <title>PyPI Recent Updates</title>
  <link>https://pypi.python.org/pypi</link>
  <description>Recent updates to the Python Package Index</description>
  <language>en</language>
 </channel>
</rss>
"""


WAREHOUSE_UPLOAD_MIGRATION_URL = "https://packaging.python.org/guides/migrating-to-pypi-org/#uploading"

AUTHOMATIC_CONFIG = {
        'google': {
            'id': 1,
            'class_': oauth2.Google,
            'consumer_key': conf.google_consumer_id,
            'consumer_secret': conf.google_consumer_secret,
            'scope': ['email', 'openid', 'profile'],
            'redirect_uri': conf.baseurl+'/google_login',
        },
        'oi': {
            'class_': openid.OpenID,
        },
}

authomatic = authomatic.Authomatic(
        config=AUTHOMATIC_CONFIG,
        secret=conf.authomatic_secret,
        logging_level=logging.CRITICAL,
        secure_cookie=conf.authomatic_secure,
    )

# Must begin and end with an alphanumeric, interior can also contain ._-
safe_username = re.compile(r"^([A-Z0-9]|[A-Z0-9][A-Z0-9._-]*[A-Z0-9])$", re.I)

safe_email = re.compile(r'^[a-zA-Z0-9._+@-]+$')
botre = re.compile(r'^$|brains|yeti|myie2|findlinks|ia_archiver|psycheclone|badass|crawler|slurp|spider|bot|scooter|infoseek|looksmart|jeeves', re.I)

class NotFound(Exception):
    pass
class Gone(Exception):
    pass
class Unauthorised(Exception):
    pass
class UnauthorisedForm(Exception):
    pass
class UserNotFound(Exception):
    pass
class Forbidden(Exception):
    pass
class Redirect(Exception):
    pass
class RedirectFound(Exception):# 302
    pass
class RedirectTemporary(Exception): # 307
    pass
class FormError(Exception):
    pass
class BlockedIP(Exception):
    pass
class MultipleReleases(Exception):
    def __init__(self, releases):
        self.releases = releases

__version__ = '1.1'

providers = (('Launchpad', 'https://launchpad.net/@@/launchpad.png', 'https://login.launchpad.net/'),)

# email sent to user indicating how they should complete their registration
rego_message = '''Subject: Complete your PyPI registration
From: %(admin)s
To: %(email)s

To complete your registration of the user "%(name)s" with the python module
index, please visit the following URL:

  %(url)s?:action=user&otk=%(otk)s

'''

# password change request email
password_change_message = '''Subject: PyPI password change request
From: %(admin)s
To: %(email)s

Someone, perhaps you, has requested that the password be changed for your
username, "%(name)s". If you wish to proceed with the change, please follow
the link below:

  %(url)s?:action=pw_reset&otk=%(otk)s

This will present a form in which you may set your new password.
'''

_prov = '<p>You may also login using <a href="/openid_login">OpenID</a>'
for title, favicon, login in providers:
    _prov += '''
    <a href="/openid_login?provider=%s"><img src="%s" title="%s"/></a>
    ''' %  (title, favicon, title)
_prov += "</p>"
unauth_message = '''
<p>If you are a new user, <a href="%(url_path)s?:action=register_form">please
register</a>.</p>
<p>If you have forgotten your password, you can have it
<a href="%(url_path)s?:action=forgotten_password_form">reset for you</a>.</p>
''' + _prov

blocked_ip_message = '''
You have attempted too many logins from this IP address.  Please try again
later.
'''

chars = 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789'

class Provider:
    def __init__(self, name, favicon, url):
        self.name = self.title = name
        self.favicon = favicon
        self.url = url

class _PyPiPageTemplate(PageTemplateFile):
    def pt_getContext(self, args=(), options={}, **kw):
        """Add our data into ZPT's defaults"""
        rval = PageTemplateFile.pt_getContext(self, args=args)
        options.update(rval)
        return options

cache_templates = True
if cache_templates:
    template_cache = {}
    def PyPiPageTemplate(file, dir):
        try:
            return template_cache[(file, dir)]
        except KeyError:
            t = _PyPiPageTemplate(file, dir)
            template_cache[(file, dir)] = t
            return t
else:
    PyPiPageTemplate = _PyPiPageTemplate

class FileUpload:
    pass

# poor man's markup heuristics so we don't have to use <PRE>,
# for when rst didn't work on the text...
br_patt = re.compile(" *\r?\n\r?(?= +)")
p_patt = re.compile(" *\r?\n(\r?\n)+")
def newline_to_br(text):
    text = re.sub(br_patt, "<BR/>", text)
    return re.sub(p_patt, "\n<P>\n", text)

def path2str(path):
    return " :: ".join(path)

def str2path(s):
    return [ node.strip() for node in s.split("::") ]


def transmute(field):
    if hasattr(field, 'filename') and field.filename:
        v = FileUpload()
        v.filename = field.filename
        v.value = field.value
        v.type = field.type
    else:
        v = field.value.decode('utf-8')
    return v

def decode_form(form):
    d = {}
    if not form:
        return d
    for k in form.keys():
        v = form[k]
        if isinstance(v, list):
            d[k] = [transmute(i) for i in v]
        else:
            d[k] = transmute(v)
    return d


def must_tls(fn):
    @functools.wraps(fn)
    def wrapped(self, *args, **kwargs):
        if self.env.get('HTTP_X_FORWARDED_PROTO') != 'https':
            raise Forbidden("Must access using HTTPS instead of HTTP")
        return fn(self, *args, **kwargs)
    return wrapped


class NoDirS3FS(fs.s3fs.S3FS):

    @property
    def _s3conn(self):
        try:
            (c,ctime) = self._tlocal.s3conn
            if time.time() - ctime > 60:
                raise AttributeError
            return c
        except AttributeError:
            c = boto.s3.connect_to_region(
                "us-west-2",
                aws_access_key_id=self._access_keys[0],
                aws_secret_access_key=self._access_keys[1],
            )
            self._tlocal.s3conn = (c,time.time())
            return c

    def makedir(self, *args, **kwargs):
        pass  # Noop this, S3 doesn't need directories

    def removedir(self, *args, **kwargs):
        pass  # Noop this, S3 doesn't need directories


def _simple_body_internal(path, urls):
    """
    Inner method for testing sql injections of requires_python
    """
    html = []
    html.append("""<!DOCTYPE html><html><head><title>Links for %s</title></head>"""
                % cgi.escape(path))
    html.append("<body><h1>Links for %s</h1>" % cgi.escape(path))
    for href, rel, text, requires_python in urls:
        if href.startswith('http://cheeseshop.python.org/pypi') or \
                href.startswith('http://pypi.python.org/pypi') or \
                href.startswith('http://www.python.org/pypi'):
            # Suppress URLs that point to us
            continue
        if rel:
            rel = ' rel="{}"'.format(cgi.escape(rel, quote=True))
        else:
            rel = ''
        href = cgi.escape(href, quote=True)
        text = cgi.escape(text)
        data_attr = ''
        if requires_python:
            data_attr = ' data-requires-python="{}"'.format(cgi.escape(requires_python, quote=True))
        html.append("""<a{} href="{}"{}>{}</a><br/>\n""".format(data_attr, href, rel, text))
    html.append("</body></html>")
    html = ''.join(html)
    return html


class WebUI:
    ''' Handle a request as defined by the "env" parameter. "handler" gives
        access to the user via rfile and wfile, and a few convenience
        functions (see pypi).

        The handling of a request goes as follows:
        1. open the database
        2. see if the request is supplied with authentication information
        3. perform the action defined by :action ("home" if none is supplied)
        4a. handle exceptions sanely, including special ones like NotFound,
            Unauthorised, Redirect and FormError, or
        4b. commit changes to the database
        5. close the database to finish off

    '''
    def __init__(self, handler, env):
        self.handler = handler
        self.config = handler.config
        self.wfile = handler.wfile
        self.sentry_client = None

        self.redis_kwargs = {
            'socket_connect_timeout': 0.1,
            'socket_timeout': 0.05,
        }

        if self.config.sentry_dsn:
            self.sentry_client = raven.Client(self.config.sentry_dsn)
        if self.config.count_redis_url:
            self.count_redis = redis.Redis.from_url(self.config.count_redis_url, **self.redis_kwargs)
        else:
            self.count_redis = None
        if self.config.queue_redis_url:
            self.queue_redis = redis.Redis.from_url(self.config.queue_redis_url, **self.redis_kwargs)
            self.queue = rq.Queue(connection=self.queue_redis)
        else:
            self.queue = None

        if self.config.cache_redis_url:
            self.cache_redis = redis.StrictRedis.from_url(self.config.cache_redis_url, **self.redis_kwargs)
        else:
            self.cache_redis = None

        # block redis is used to store blocked users, IPs, etc to prevent brute
        # force attacks
        if self.config.block_redis_url:
            self.block_redis = redis.Redis.from_url(self.config.block_redis_url, **self.redis_kwargs)
        else:
            self.block_redis = None

        self.env = env
        self.nav_current = None
        self.privkey = None
        self.username = None
        self.authenticated = False # was a password or a valid cookie passed?
        self.loggedin = False      # was a valid cookie sent?
        self.usercookie = None
        self.failed = None # error message if initialization already produced a failure


        self.s3conn = boto.s3.connect_to_region(
            "us-west-2",
            aws_access_key_id=self.config.database_aws_access_key_id,
            aws_secret_access_key=self.config.database_aws_secret_access_key,
        )

        self.package_bucket = self.s3conn.get_bucket(
            self.config.database_files_bucket,
            validate=False,
        )

        if self.config.database_docs_bucket is not None:
            self.docs_fs = NoDirS3FS(
                bucket=self.config.database_docs_bucket,
                aws_access_key=self.config.database_aws_access_key_id,
                aws_secret_key=self.config.database_aws_secret_access_key,
            )
        else:
            self.docs_fs = fs.osfs.OSFS(self.config.database_docs_dir)

        # XMLRPC request or not?
        if self.env.get('CONTENT_TYPE') != 'text/xml':
            fstorage = cgi.FieldStorage(fp=handler.rfile, environ=env)
            try:
                self.form = decode_form(fstorage)
            except UnicodeDecodeError:
                self.failed = "Form data is not correctly encoded in UTF-8"
        else:
            self.form = None

        # figure who the end user is
        self.remote_addr = self.env['REMOTE_ADDR']
        if env.get('HTTP_X_FORWARDED_FOR'):
            # X-Forwarded-For: client1, proxy1, proxy2
            self.remote_addr = self.env['HTTP_X_FORWARDED_FOR'].split(',')[0]

        # set HTTPS mode if we're directly or indirectly (proxy) supposed to be
        # serving HTTPS links
        if env.get('HTTP_X_FORWARDED_PROTO') == 'https':
            self.config.make_https()
        else:
            self.config.make_http()

        (protocol, machine, path, x, x, x) = urlparse.urlparse(self.config.url)
        self.url_machine = '%s://%s'%(protocol, machine)
        self.url_path = path

        # configure logging
        if self.config.logfile or self.config.mail_logger or self.config.sentry_dsn:
            root = logging.getLogger()
            root.setLevel(logging.WARNING)

            if self.config.logfile:
                hdlr = logging.FileHandler(self.config.logfile)
                formatter = logging.Formatter(
                    '%(asctime)s %(name)s:%(levelname)s %(message)s')
                hdlr.setFormatter(formatter)
                root.handlers.append(hdlr)
            if self.config.mail_logger:
                smtp_starttls = None
                if self.config.smtp_starttls:
                    smtp_starttls = ()
                smtp_credentials = None
                if self.config.smtp_auth:
                    smtp_credentials = (self.config.smtp_login, self.config.smtp_password)
                hdlr = MailingLogger.MailingLogger(self.config.smtp_hostname,
                                                   self.config.fromaddr,
                                                   self.config.toaddrs,
                                                   '[PyPI] %(line)s',
                                                   credentials=smtp_credentials,
                                                   secure=smtp_starttls,
                                                   send_empty_entries=False,
                                                   flood_level=10)
                root.handlers.append(hdlr)
            if self.config.sentry_dsn:
                root.handlers.append(SentryHandler(self.sentry_client))

    def run(self):
        ''' Run the request, handling all uncaught errors and finishing off
            cleanly.
        '''
        if self.failed:
            # failed during initialization
            self.fail(self.failed)
            return
        self.store = store.Store(
            self.config,
            queue=self.queue,
            redis=self.count_redis,
            package_bucket=self.package_bucket,
        )
        self.statsd = statsd_client()
        self.dogstatsd = dogstatsd
        try:
            try:
                self.store.get_cursor() # make sure we can connect
                self.inner_run()
            except NotFound, err:
                self.fail('Not Found (%s)' % err, code=404)
            except Gone, err:
                self.fail('Gone (%s)' % err, code=410, headers={"Cache-Control": "max-age=31557600, public"})
            except Unauthorised, message:
                message = str(message)
                if not message:
                    message = 'You must login to access this feature'
                msg = unauth_message%self.__dict__
                self.fail(message, code=401, heading='Login required',
                    content=msg, headers={'WWW-Authenticate':
                    'Basic realm="pypi"'})
            except UnauthorisedForm, message:
                message = str(message)
                if not message:
                    message = 'You must login to access this feature'
                msg = unauth_message%self.__dict__
                self.fail(message, code=401, content=msg)
            except Forbidden, message:
                message = str(message)
                self.fail(message, code=403, heading='Forbidden')
            except Redirect, e:
                self.handler.send_response(301, 'Moved Permanently')
                self.handler.send_header('Location', e.args[0].encode("utf8"))
                self.handler.end_headers()
            except RedirectFound, e:
                self.handler.send_response(302, 'Found')
                self.handler.send_header('Location', e.args[0].encode("utf8"))
                self.handler.end_headers()
            except RedirectTemporary, e:
                # ask browser not to cache this redirect
                self.handler.send_response(307, 'Temporary Redirect')
                self.handler.send_header('Location', e.args[0].encode("utf8"))
                self.handler.send_header('Cache-Control', 'max-age=0')
                self.handler.end_headers()
            except BlockedIP:
                msg = blocked_ip_message % self.__dict__
                self.fail(msg, code=403, heading='Blocked IP')
            except FormError, message:
                message = str(message)
                self.fail(message, code=400, heading='Error processing form')
            except IOError, error:
                # ignore broken pipe errors (client vanished on us)
                if error.errno != 32: raise
            except OperationalError, message:
                # clean things up
                self.store.force_close()
                message = str(message)
                self.fail('Please try again later.\n<!-- %s -->'%message,
                    code=500, heading='Database connection failed')
            except:
                exc, value, tb = sys.exc_info()
                real_tb = traceback.format_exc()

                # attempt to send all the exceptions to Raven
                try:
                    from raven.utils.serializer import transform

                    if self.sentry_client:
                        if self.form and not isinstance(self.form, FileUpload):
                            form_data = self.form
                        else:
                            form_data = ""

                        self.sentry_client.captureException(
                            data={
                                "sentry.interfaces.Http": {
                                    "method": self.env.get("REQUEST_METHOD"),
                                    "url": raven.utils.wsgi.get_current_url(
                                        self.env,
                                        strip_querystring=True,
                                    ),
                                    "query_string": self.env.get(
                                        "QUERY_STRING",
                                    ),
                                    "data": transform(form_data),
                                    "headers": dict(
                                        raven.utils.wsgi.get_headers(self.env),
                                    ),
                                    "env": dict(
                                        raven.utils.wsgi.get_environ(self.env),
                                    ),
                                }
                            },
                        )
                except Exception:
                    # sentry broke so just email the exception like old times
                    if ('connection limit exceeded for non-superusers'
                            not in str(value)):
                        logging.exception('Internal Error\n----\n%s\n----\n%s\n----\n' % (
                            '\n'.join(['%s: %s' % x for x in self.env.items()]),
                            real_tb,
                        ))

                if self.config.debug_mode == 'yes':
                    s = cStringIO.StringIO()
                    traceback.print_exc(None, s)
                    s = cgi.escape(s.getvalue())
                    self.fail('Internal Server Error', code=500,
                        heading='Error...', content='%s'%s)
                else:
                    s = '%s: %s'%(exc, value)
                    self.fail("There's been a problem with your request",
                        code=500, heading='Error...', content='%s'%s)
        finally:
            self.store.close()

    # these are inserted at the top of the standard template if set
    error_message = None
    ok_message = None

    def write_plain(self, payload):
        self.handler.send_response(200)
        self.handler.send_header("Content-type", 'text/plain')
        self.handler.send_header("Content-length", str(len(payload)))
        self.handler.end_headers()
        self.handler.wfile.write(payload)

    def write_template(self, filename, headers={}, **options):
        context = {}
        options.setdefault('norobots', False)
        options.setdefault('keywords', 'python programming language object'
            ' oriented web free source package index download software')
        options.setdefault('description', 'The Python Package Index is a'
            ' repository of software for the Python programming language.')
        options['providers'] = self.get_providers()
        context['data'] = options
        context['app'] = self
        fpi = self.config.url+self.env.get('PATH_INFO',"")
        try:
            options['FULL_PATH_INFO'] = fpi.decode("utf-8")
        except UnicodeError:
            raise NotFound, fpi + ' is not utf-8 encoded'

        template_dir = os.path.join(os.path.dirname(__file__), 'templates')
        context['standard_template'] = PyPiPageTemplate(
            "standard_template.pt", template_dir)
        template = PyPiPageTemplate(filename, template_dir)
        content = template(**context)

        # dynamic insertion of CSRF token into FORMs
        if '"POST"' in content and self.authenticated:
            token = '<input type="hidden" name="CSRFToken" value="%s">' % (
                    self.store.get_token(self.username),)
            temp = content.split('\n')
            edit = ((i, l) for i, l in enumerate(content.split('\n')) if
                    '"POST"' in l)
            try:
                for index, line in edit:
                    while not line.endswith('>'):
                        index += 1
                        line = temp[index]
                    # count spaces to align entry nicely
                    spaces = len(line.lstrip()) - len(line)
                    temp[index] = "\n".join((line, ' ' * spaces + token))
                content = '\n'.join(temp)
            except IndexError:
                # this should not happen with correct HTML syntax
                # the try is 'just in case someone does something stupid'
                pass

        self.handler.send_response(200, 'OK')
        if 'content-type' in options:
            self.handler.set_content_type(options['content-type'])
        else:
            self.handler.set_content_type('text/html; charset=utf-8')
        if self.usercookie:
            if self.url_machine.startswith('https'):
                secure = ';secure'
            else:
                secure = ''

            self.handler.send_header('Set-Cookie',
                'pypi=%s;path=/%s' % (self.usercookie, secure))
        for k,v in headers.items():
            self.handler.send_header(k, v)
        self.handler.end_headers()
        self.wfile.write(content.encode('utf-8'))

    def fail(self, message, title="Python Package Index", code=400,
            heading=None, headers={}, content=''):
        ''' Indicate to the user that something has failed.
        '''
        if isinstance(message, unicode):
            message = message.encode("utf-8")

        self.handler.send_response(code, message)
        if '<' in content and '>' in content:
            html = True
            self.handler.set_content_type('text/html; charset=utf-8')
        else:
            html = False
            self.handler.set_content_type('text/plain; charset=utf-8')

        for k,v in headers.items():
            self.handler.send_header(k, v)
        self.handler.end_headers()

        if heading:
            if html:
                self.wfile.write('<strong>' + heading +
                    '</strong><br /><br />\n\n')
            else:
                self.wfile.write(heading + '\n\n')
        self.wfile.write(message)
        if html: self.wfile.write('<br /><br />\n')
        else: self.wfile.write('\n\n')
        self.wfile.write(content)

    def link_action(self, action_name=None, **vars):
        if action_name:
            vars[':action'] = action_name
        l = []
        for k,v in vars.items():
            l.append('%s=%s'%(urllib.quote(k.encode('utf-8')),
                urllib.quote(v.encode('utf-8'))))
        return self.url_path + '?' + '&'.join(l)

    navlinks = (
        ('browse', 'Browse packages'),
        ('list_classifiers', 'List trove classifiers'),
        ('rss', 'RSS (latest 40 updates)'),
        ('packages_rss', 'RSS (newest 40 packages)'),
        ('role_form', 'Admin'),
    )
    def navlinks_html(self):
        links = []
        for action_name, desc in self.navlinks:
            desc = desc.replace(' ', '&nbsp;')
            if action_name == 'role_form' and (
                not self.username or not self.store.has_role('Admin', '')):
                continue

            cssclass = ''
            if action_name == self.nav_current:
                cssclass = 'selected'
            links.append('<li class="%s"><a class="%s" href="%s">%s</a></li>' %
                         (cssclass, cssclass, self.link_action(action_name), desc))
        return links

    def inner_run(self):
        ''' Figure out what the request is, and farm off to the appropriate
            handler.
        '''
        # See if this is the "simple" pages and signatures
        script_name = self.env.get('SCRIPT_NAME')
        if script_name and script_name == self.config.simple_script:
            return self.run_simple()
        if script_name and script_name == self.config.simple_sign_script:
            return self.run_simple_sign()
        # if script_name == '/packages':
        #     return self.packages()
        if script_name == '/mirrors':
            return self.mirrors()
        if script_name == '/security':
            return self.security()
        if script_name == '/tos':
            return self.tos()
        if script_name == '/daytime':
            return self.daytime()
        if script_name == '/serial':
            return self.current_serial()

        # on logout, we set the cookie to "logged_out"
        self.cookie = Cookie.SimpleCookie(self.env.get('HTTP_COOKIE', ''))
        try:
            self.usercookie = self.cookie['pypi'].value
        except KeyError:
            self.usercookie = None

        name = self.store.find_user_by_cookie(self.usercookie)
        if name:
            self.loggedin = True
            self.authenticated = True # implied by loggedin
            self.username = name
            # no login time update, since looking for the
            # cookie did that already
            self.store.set_user(name, self.remote_addr, False)
        else:
            # see if the user has provided a username/password
            auth = self.env.get('HTTP_CGI_AUTHORIZATION', '').strip()
            if auth:
                if not self._check_blocked_ip():
                    try:
                        self._handle_basic_auth(auth)
                    except (Unauthorised, UserNotFound):
                        # if either an invalid user or password was set,
                        # increase the IP's failed login count
                        self._failed_login_ip()
                else:
                    raise BlockedIP

            else:
                un = self.env.get('SSH_USER', '')
                if un and self.store.has_user(un):
                    user = self.store.get_user(un)
                    self.username = un
                    self.authenticated = self.loggedin = True
                    last_login = user['last_login']
                    # Only update last_login every minute
                    update_last_login = not last_login or (time.time()-time.mktime(last_login.timetuple()) > 60)
                    self.store.set_user(un, self.remote_addr, update_last_login)

        # Commit all user-related changes made up to here
        if self.username:
            self.store.commit()

        # Now we have a username try running OAuth if necessary
        if script_name == '/oauth':
            raise Gone, "OAuth has been disabled."
        if script_name == '/google_login':
            return self.google_login()
        if script_name == '/openid_login':
            return self.openid_login()
        if script_name == '/openid_claim':
            return self.openid_claim()


        if self.env.get('CONTENT_TYPE') == 'text/xml':
            self.xmlrpc()
            return

        # now handle the request
        path = self.env.get('PATH_INFO', '')
        if self.form.has_key(':action'):
            action = self.form[':action']
            if isinstance(action, list):
                raise FormError("Multiple actions not allowed: %r" % action)
        elif path:
            # Split into path items, drop leading slash
            try:
                items = path.decode('utf-8').split('/')[1:]
            except UnicodeError:
                raise NotFound(path + " is not UTF-8 encoded")
            action = None
            if path == '/':
                self.form['name'] = ''
                action = 'index'
            elif len(items) >= 1:
                self.form['name'] = items[0]
                action = 'display'
            if len(items) >= 2 and items[1]:
                self.form['version'] = items[1]
                action = 'display'
            if len(items) == 3 and items[2]:
                action = self.form[':action'] = items[2]
            if not action:
                raise NotFound
        else:
            action = 'home'

        if self.form.get('version') in ('doap', 'json'):
            action, self.form['version'] = self.form['version'], None

        # make sure the user has permission
        if action in ('submit', ):
            if not self.authenticated:
                raise Unauthorised
            if self.store.get_otk(self.username):
                raise Unauthorised, "Incomplete registration; check your email"
            if not self.store.user_active(self.username):
                raise Unauthorised("Inactive User")

        # handle the action
        if action in '''home browse rss index search submit doap
        display_pkginfo submit_pkg_info remove_pkg pkg_edit verify
        display register_form user user_form
        forgotten_password_form forgotten_password
        password_reset pw_reset pw_reset_change
        role role_form list_classifiers login logout files
        file_upload show_md5 doc_upload doc_destroy dropid
        clear_auth addkey delkey lasthour json gae_file about delete_user
        rss_regen packages_rss
        exception login_form purge'''.split():
            self.dogstatsd.increment('dispatch_action', tags=['action:{}'.format(action)])
            getattr(self, action)()
        else:
            #raise NotFound, 'Unknown action %s' % action
            raise NotFound

        if action in ['pkg_edit', 'remove_pkg']:
            self.store.enqueue(tasks.rss_regen,)

        # commit any database changes
        self.store.commit()

    def _check_credentials(self, username, password):

        if not self.store.has_user(username):
            self.dogstatsd.increment('authentication.failure', tags=['method:password', 'reason:no_user'])
            raise UserNotFound

        if self._check_blocked_user(username):
            username = password = ''
            self.dogstatsd.increment('authentication.failure', tags=['method:password', 'reason:blocked_user'])
            raise UserNotFound


        # Fetch the user from the database
        user = self.store.get_user(username)

        # Verify the hash, and see if it needs migrated
        ok, new_hash = self.config.passlib.verify_and_update(password, user["password"])

        # If our password didn't verify as ok then raise an
        #   error.
        if not ok:
            self._failed_login_user(username)
            self.dogstatsd.increment('authentication.failure', tags=['method:password', 'reason:incorrect_password'])
            raise Unauthorised, 'Incorrect password'

        if new_hash:
            # The new hash needs to be stored for this user.
            self.store.setpasswd(username, new_hash, hashed=True)

        # Login the user
        self.username = username
        self.authenticated = True

        # Determine if we need to store the users last login,
        #   as we only want to do this once a minute.
        last_login = user['last_login']
        update_last_login = not last_login or (time.time()-time.mktime(last_login.timetuple()) > 60)
        self.dogstatsd.increment('authentication.success', tags=['method:password'])
        self.store.set_user(username, self.remote_addr, update_last_login)

    def _handle_basic_auth(self, auth):
        if not auth.lower().startswith('basic '):
            return
        self.dogstatsd.increment('authentication.start', tags=['method:basic_auth'])

        authtype, auth = auth.split(None, 1)
        try:
            username, password = base64.decodestring(auth).split(':', 1)
        except (binascii.Error, ValueError):
            # Invalid base64, or no colon
            username = password = ''

        self._check_credentials(username, password)

        self.statsd.incr('password_authentication.basic_auth')
        self.dogstatsd.increment('authentication.complete', tags=['method:basic_auth'])

    def login_form(self):
        if self.env['REQUEST_METHOD'] == "POST":
            self.dogstatsd.increment('authentication.start', tags=['method:login_form'])
            nonce = self.form.get('nonce', '')
            username = self.form.get('username', '')
            password = self.form.get('password', '')

            cookies = dict([(k, v.value) for k, v in Cookie.SimpleCookie(self.env.get('HTTP_COOKIE', '')).items()])
            if nonce != cookies.get('login_nonce', None):
                raise FormError, "Form Failure; reset form submission"

            if not self._check_blocked_ip():
                try:
                    self._check_credentials(username, password)
                except (Unauthorised, UserNotFound):
                    self._failed_login_ip()
                    raise UnauthorisedForm, 'Incorrect password'
                    self.home()
            else:
                raise BlockedIP

            self.statsd.incr('password_authentication.login_form')
            self.dogstatsd.increment('authentication.complete', tags=['method:login_form'])

            self.usercookie = self.store.create_cookie(self.username)
            self.store.get_token(self.username)
            self.loggedin = 1
            self.home()

        elif self.env['REQUEST_METHOD'] == "GET":
            nonce = store.generate_random(30)
            headers = {'Set-Cookie': 'login_nonce=%s;secure' % (nonce),
                       'X-FRAME-OPTIONS': 'DENY'}
            self.write_template('login.pt', title="PyPI Login",
                                headers=headers, nonce=nonce)
        else:
            self.handler.send_response(405, 'Method Not Allowed')

    def _failed_login_ip(self):
        if self.block_redis:
            try:
                if not self.block_redis.exists(self.remote_addr):
                    self.block_redis.set(self.remote_addr, 1)
                    self.block_redis.expire(self.remote_addr,
                                            int(self.config.blocked_timeout))
                else:
                    self.block_redis.incr(self.remote_addr)
            except redis.ConnectionError:
                pass

    def _failed_login_user(self, username):
        if self.block_redis:
            try:
                if not self.block_redis.exists(username):
                    self.block_redis.set(username, 1)
                    self.block_redis.expire(username,
                                            int(self.config.blocked_timeout))
                else:
                    self.block_redis.incr(username)
            except redis.ConnectionError:
                pass

    def _check_blocked_ip(self):
        if self.block_redis:
            try:
                if (self.block_redis.exists(self.remote_addr) and
                        int(self.block_redis.get(self.remote_addr)) >
                        int(self.config.blocked_attempts_ip)):
                    return True
            except redis.ConnectionError:
                return False
        return False

    def _check_blocked_user(self, username):
        if self.block_redis:
            try:
                if (self.block_redis.exists(username) and
                        int(self.block_redis.get(username)) >
                        int(self.config.blocked_attempts_user)):
                    return True
            except redis.ConnectionError:
                return False
        return False

    def exception(self):
        FAIL

    @must_tls
    def xmlrpc(self):
        rpc.handle_request(self)

    @must_tls
    def purge(self):
        projects = self.form.get("project", [])
        if not isinstance(projects, list):
            projects = [projects]
        for project in projects:
            self.store._add_invalidation(project)

        self.write_plain("OK")


    def simple_body(self, path):
        # Check to see if we're using the normalized name or not.
        if path != safe_name(path).lower():
            names = self.store.find_package(path)
            if names:
                target_url = "/".join([
                    self.config.simple_script,
                    safe_name(path).lower(),
                ])
                raise Redirect, target_url
            else:
                raise NotFound, path + " does not have any releases"

        names = self.store.find_package(path)
        if names:
            name = names[0]
        else:
            raise NotFound, path + " does not exist"

        urls = self.store.get_package_urls(name, relative="../../packages")

        if urls is None:
            raise NotFound, path + " does not have any releases"

        return _simple_body_internal(path, urls)

    def get_accept_encoding(self, supported):
        accept_encoding = self.env.get('HTTP_ACCEPT_ENCODING')
        if not accept_encoding:
            return None
        accept_encoding = accept_encoding.split(',')
        result = {}
        for s in accept_encoding:
            s = s.split(';') # gzip;q=0.6
            if len(s) == 1:
                result[s[0].strip()] = 1.0
            elif s[1].startswith('q='):
                result[s[0].strip()] = float(s[1][2:])
            else:
                # not the correct format
                result[s[0].strip()] = 1.0
        best_prio = 0
        best_enc = None
        for enc in supported:
            if enc in result:
                prio = result[enc]
            elif '*' in result:
                prio = result['*']
            else:
                prio = 0
            if prio > best_prio:
                best_prio, best_enc = prio, enc
        return best_enc

    @must_tls
    def run_simple(self):
        self.store.set_read_only()
        path = self.env.get('PATH_INFO')

        if not path:
            raise Redirect(self.config.simple_script + '/')

        if path == '/':
            html = [
                '<html><head><title>Simple Index</title><meta name="api-version" value="2" /></head>',
                "<body>\n",
            ]

            html.extend(
                "<a href='%s'>%s</a><br/>\n" % (
                    urllib.quote(safe_name(name).lower()),
                    cgi.escape(name),
                )
                for name in self.store.get_packages_utf8()
            )

            html.append("</body></html>")
            html = ''.join(html)

            self.handler.send_response(200, 'OK')
            self.handler.set_content_type('text/html; charset=utf-8')
            self.handler.send_header('Content-Length', str(len(html)))
            self.handler.send_header("Surrogate-Key", "simple simple-index")
            # XXX not quite sure whether this is the right thing for empty
            # mirrors, but anyway.
            serial = self.store.changelog_last_serial() or 0
            self.handler.send_header("X-PYPI-LAST-SERIAL", str(serial))
            self.handler.end_headers()
            self.wfile.write(html)
            return

        path = path[1:]
        if not path.endswith('/'):
            raise Redirect(self.config.simple_script + '/' + path + '/')
        path = path[:-1]

        if '/' in path:
            raise NotFound(path)

        html = self.simple_body(path)

        # Make sure we're using the cannonical name.
        names = self.store.find_package(path)
        if names:
            path = names[0]

        serial = self.store.last_serial_for_package(path)
        self.handler.send_response(200, 'OK')
        self.handler.set_content_type('text/html; charset=utf-8')
        self.handler.send_header('Content-Length', str(len(html)))
        self.handler.send_header("Surrogate-Key", "simple pkg~%s" % safe_name(path).lower())
        self.handler.send_header("X-PYPI-LAST-SERIAL", str(serial))
        self.handler.end_headers()
        self.wfile.write(html)

    def run_simple_sign(self):
        raise Gone(
            "The Simple Sign API has been deprecated and removed. If you're "
            "mirroring PyPI with bandersnatch then please upgrade to 1.7+. "
            "If you're mirroring PyPI with pep381client then please switch to "
            "bandersnatch. Otherwise contact the maintainer of your software "
            "and inform them of PEP 464."
        )

    @must_tls
    def packages(self):
        self.store.set_read_only()
        path = self.env.get('PATH_INFO')
        parts = path.split("/")

        if len(parts) < 5 and not path.endswith("/"):
            raise Redirect("/packages" + path + "/")

        filename = os.path.basename(path)
        possible_package = os.path.basename(os.path.dirname(path))
        file_key = None
        file_chunk = None

        headers = {}
        status = (200, "OK")

        if filename:
            md5_digest = self.store.get_digest_from_filename(filename)

            if md5_digest:
                headers["ETag"] = '"%s"' % md5_digest
                if md5_digest == self.env.get("HTTP_IF_NONE_MATCH"):
                    status = (304, "Not Modified")

            # Make sure that we associate the delivered file with the serial this
            # is valid for. Intended to support mirrors to more easily achieve
            # consistency with files that are newer than they may expect.
            package = self.store.get_package_from_filename(filename)
            if package:
                serial = self.store.last_serial_for_package(package)
                if serial is not None:
                    headers["X-PyPI-Last-Serial"] = str(serial)

                possible_package = package

            if md5_digest:
                headers["ETag"] = '"%s"' % md5_digest

            if status[0] != 304:
                file_key = self.package_bucket.get_key(path, validate=False)
                try:
                    file_chunk = file_key.read(4096)
                except boto.exception.S3ResponseError as exc:
                    if exc.error_code != "NoSuchKey":
                        raise
                    status = (404, "Not Found")
                else:
                    headers["Content-Type"] = "application/octet-stream"

        headers["Surrogate-Key"] = "package pkg~%s" % safe_name(possible_package).lower()

        self.handler.send_response(*status)

        for key, value in headers.items():
            self.handler.send_header(key, value)

        self.handler.end_headers()

        if file_key is not None and file_chunk is not None:
            while file_chunk:
                self.wfile.write(file_chunk)
                file_chunk = file_key.read(4096)

    def home(self, nav_current='home'):
        self.write_template('home.pt', title='PyPI - the Python Package Index',
                            headers={'X-XRDS-Location':self.url_machine+'/id'})

    def about(self, nav_current='home'):
        self.write_template('about.pt', title='About PyPI')

    def rss(self):
        """Dump the last N days' updates as an RSS feed.
        """
        # determine whether the rss file is up to date
        content = None
        if self.cache_redis is None:
            content = EMPTY_RSS
        else:
            try:
                value = self.cache_redis.get('rss~main')
                if value:
                    content = value
                else:
                    tasks.rss_regen()
                    content = self.cache_redis.get('rss~main')
            except redis.ConnectionError:
                content = EMPTY_RSS

        # TODO: throw in a last-modified header too?
        self.handler.send_response(200, 'OK')
        self.handler.set_content_type('text/xml; charset=utf-8')
        self.handler.end_headers()
        self.wfile.write(content)

    def packages_rss(self):
        """Dump the last N days' updates as an RSS feed.
        """
        # determine whether the rss file is up to date
        content = None
        if self.cache_redis is None:
            content = EMPTY_RSS
        else:
            try:
                value = self.cache_redis.get('rss~pkgs')
                if value:
                    content = value
                else:
                    tasks.rss_regen()
                    content = self.cache_redis.get('rss~pkgs')
            except redis.ConnectionError:
                content = EMPTY_RSS

        # TODO: throw in a last-modified header too?
        self.handler.send_response(200, 'OK')
        self.handler.set_content_type('text/xml; charset=utf-8')
        self.handler.end_headers()
        self.wfile.write(content)

    def rss_regen(self):
        context = {}
        context['app'] = self
        context['test'] = ''
        if 'testpypi' in self.config.url:
            context['test'] = 'Test '

        # generate the releases RSS
        template_dir = os.path.join(os.path.dirname(__file__), 'templates')
        template = PyPiPageTemplate('rss.xml', template_dir)
        content = template(**context)
        f = open(self.config.rss_file, 'w')
        try:
            f.write(content.encode('utf-8'))
        finally:
            f.close()

        # generate the packages RSS
        template_dir = os.path.join(os.path.dirname(__file__), 'templates')
        template = PyPiPageTemplate('packages-rss.xml', template_dir)
        content = template(**context)
        f = open(self.config.packages_rss_file, 'w')
        try:
            f.write(content.encode('utf-8'))
        finally:
            f.close()

    def lasthour(self):
        self.write_template('rss1hour.xml', **{'content-type':'text/xml; charset=utf-8'})

    def browse(self, nav_current='browse'):
        ua = self.env.get('HTTP_USER_AGENT', '')
        if botre.search(ua) is not None:
            self.handler.send_response(200, 'OK')
            self.handler.set_content_type('text/plain')
            self.handler.end_headers()
            self.wfile.write('This page intentionally blank.')
            return

        self.nav_current = nav_current

        trove = self.store.trove()
        qs = cgi.parse_qsl(self.env.get('QUERY_STRING', ''))

        # Analyze query parameters c= and show=
        cat_ids = []
        show_all = False
        for x in qs:
            if x[0] == 'c':
                try:
                    c = int(x[1])
                except:
                    continue
                if trove.trove.has_key(c):
                    cat_ids.append(c)
            elif x[0] == 'show' and x[1] == 'all':
                show_all = True
        cat_ids.sort()

        # XXX with 18 classifiers, postgres runs out of memory
        # So limit the number of simultaneous classifiers
        if len(cat_ids) > 8:
            self.fail("Too many classifiers", code=400)
            return

        # Fetch data from the database
        if cat_ids:
            packages_, tally = self.store.browse(cat_ids)
        else:
            # use cached version of top-level browse page
            packages_, tally = self.store.browse_tally()

        # we don't need the database any more, so release it
        self.store.close()

        # group tally into parent nodes
        boxes = {}
        for id, count in tally:
            if id in cat_ids:
                # Don't provide link to a selected category
                continue
            node = trove.trove[id]
            parent = ' :: '.join(node.path_split[:-1])
            boxes.setdefault(parent, []).append((node.name, id, count))
        # Sort per box alphabetically by name
        for box in boxes.values():
            box.sort()

        # order the categories; some are hardcoded to be first: topic,
        # environment, framework
        available_categories_ = []
        for cat in ("Topic", "Environment", "Framework"):
            if boxes.has_key(cat):
                available_categories_.append((cat, boxes.pop(cat)))
        # Sort the rest alphabetically
        boxes = boxes.items()
        boxes.sort()
        available_categories_.extend(boxes)

        # ... build packages viewdata
        packages_count = len(packages_)
        packages = []
        for p in packages_:
            packages.append(dict(name=p[0], version=p[1], summary=p[2],
                url=self.packageURL(p[0], p[1])))

        # ... build selected categories viewdata
        selected_categories = []
        for c in cat_ids:
            n = trove.trove[c]
            unselect_url = "%s?:action=browse" % self.url_path
            for c2 in cat_ids:
                if c==c2: continue
                unselect_url += "&c=%d" % c2
            selected_categories.append(dict(path_string=cgi.escape(n.path),
                    path = n.path_split,
                    pathstr = path2str(n.path_split),
                    unselect_url = unselect_url))

        # ... build available categories viewdata
        available_categories = []
        for name, subcategories in available_categories_:
            sub = []
            for subcategory, fid, count in subcategories:
                if fid in cat_ids:
                    add = cat_ids
                else:
                    add = cat_ids + [fid]
                add.sort()
                url = self.url_path + '?:action=browse'
                for c in add:
                    url += "&c=%d" % c
                sub.append(dict(
                    name = subcategory,
                    packages_count = count,
                    url = url,
                    description = subcategory[-1]))

            available_categories.append(dict(
                subcategories=sub, name=name, id=id))

        # only show packages if they're less than 20 and the user has
        # selected some categories, or if the user has explicitly asked
        # for them all to be shown by passing show=all on the URL
        show_packages = selected_categories and \
            (packages_count < 30 or show_all)

        # render template
        url = self.url_path + '?:action=browse&show=all'
        for c in cat_ids:
            url += '&c=%d' % c
        self.write_template('browse.pt', title="Browse",
            show_packages_url=url,
            show_packages=show_packages, packages=packages,
            packages_count=packages_count,
            selected_categories=selected_categories,
            available_categories=available_categories,
            norobots=True)

    def logout(self):
        self.loggedin = False
        self.store.delete_cookie(self.usercookie)
        self.home()

    def clear_auth(self):
        if self.username:
            raise Unauthorised, "Clearing basic auth"
        self.home()


    def login(self):
        if not self.authenticated:
            raise Unauthorised
        self.usercookie = self.store.create_cookie(self.username)
        self.store.get_token(self.username)
        self.loggedin = 1
        self.home()

    def openid_login(self):
        if 'provider' in self.form:
            for p in providers:
                if p[0] == self.form['provider']:
                    self.form['openid_identifier'] = p[2]
            self.form['id'] = None
        if 'openid_identifier' in self.form:
            self.form['id'] = self.form['openid_identifier']
        elif 'realm' not in self.form:
            self.write_template('openid.pt', title='OpenID Login')

        self.handler.set_status('200 OK')
        result = authomatic.login(PyPIAdapter(self.env, self.config, self.handler, self.form), 'oi',
                                  use_realm=False, store=self.store.oid_store(),
                                  return_url=self.config.baseurl+'/openid_login')

        if result:
            if result.user:
                content = result.user.data
                result_openid_id = content.get('guid', None)
                user = None
                if result_openid_id:
                    found_user = self.store.get_user_by_openid(result_openid_id)
                    if found_user:
                        user = found_user
                if user:
                    self.username = user['name']
                    self.loggedin = self.authenticated = True
                    self.usercookie = self.store.create_cookie(self.username)
                    self.store.get_token(self.username)
                    self.store.commit()
                    self.statsd.incr('openid.client.login')
                    self.dogstatsd.increment('authentication.complete', tags=['method:openid'])
                    self.home()
                else:
                    self.dogstatsd.increment('authentication.failure', tags=['method:openid'])
                    return self.fail('OpenID: No associated user for {0}'.format(result_openid_id))
        self.handler.end_headers()

    def openid_claim(self):
        '''Claim an OpenID.'''
        if not self.loggedin:
            return self.fail('You are not logged in')
        if 'openid_identifier' in self.form and self.env['REQUEST_METHOD'] != "POST":
            return self.fail('OpenID Claims must be POST')
        if self.env['REQUEST_METHOD'] == "POST":
            self.csrf_check()
        if 'provider' in self.form:
            for p in providers:
                if p[0] == self.form['provider']:
                    self.form['openid_identifier'] = p[2]
            self.form['id'] = None
        if 'openid_identifier' in self.form:
            self.form['id'] = self.form['openid_identifier']

        self.handler.set_status('200 OK')
        result = authomatic.login(PyPIAdapter(self.env, self.config, self.handler, self.form), 'oi',
                                  use_realm=False, store=self.store.oid_store(),
                                  return_url=self.config.baseurl+'/openid_claim')

        if result:
            if result.user:
                content = result.user.data
                result_openid_id = content.get('guid', None)
                if result_openid_id:
                    found_user = self.store.get_user_by_openid(result_openid_id)
                    if found_user:
                        return self.fail('OpenID is already claimed')
                    self.store.associate_openid(self.username, result_openid_id)
                    self.store.commit()
                    self.statsd.incr('openid.client.claim')
                    self.dogstatsd.increment('openid.client.claim')
                    self.home()
        self.handler.end_headers()


    def dropid(self):
        if not self.loggedin:
            return self.fail('You are not logged in')
        if 'openid' not in self.form:
            raise FormError, "ID missing"
        openid = self.form['openid']
        for i in self.store.get_openids(self.username):
            if openid == i['id']:break
        else:
            raise Forbidden, "You don't own this ID"
        self.store.drop_openid(openid)
        return self.register_form()

    def role_form(self):
        ''' A form used to maintain user Roles
        '''
        self.nav_current = 'role_form'
        package_name = ''
        if self.form.has_key('package_name'):
            package_name = self.form['package_name']
            if not (self.store.has_role('Admin', package_name) or
                    self.store.has_role('Owner', package_name)):
                raise Unauthorised
            package = '''
<tr><th>Package Name:</th>
    <td><input type="text" readonly name="package_name" value="%s"></td>
</tr>
'''%cgi.escape(package_name)
        elif not self.store.has_role('Admin', ''):
            raise Unauthorised
        else:
            names = [x['name'] for x in self.store.get_packages()]
            names.sort()
            names = map(cgi.escape, names)
            s = '\n'.join(['<option value="%s">%s</option>'%(name, name)
                            for name in names])
            package = '''
<tr><th>Package Name:</th>
    <td><select name="package_name">%s</select></td>
</tr>
'''%s

        self.write_template('role_form.pt', title='Role maintenance',
            name=package_name, package=package)

    def package_role_list(self, name, heading='Assigned Roles'):
        ''' Generate an HTML fragment for a package Role display.
        '''
        l = ['<table class="roles">',
             '<tr><th class="header" colspan="2">%s</th></tr>'%heading,
             '<tr><th>User</th><th>Role</th></tr>']
        for assignment in self.store.get_package_roles(name):
            username = assignment[1]
            user = self.store.get_user(username)
            l.append('<tr><td>%s</td><td>%s</td></tr>'%(
                cgi.escape(username),
                cgi.escape(assignment[0])))
        l.append('</table>')
        return '\n'.join(l)

    def _get_latest_pkg_info(self, name, version, hidden=False):
        # get the appropriate package info from the database
        if name is None:
            try:
                name = self.form['name']
            except KeyError:
                raise NotFound, 'no package name supplied'

        # Make sure that our package name is correct
        names = self.store.find_package(name)
        if names and names[0] != name:
            parts = ["pypi", names[0]]
            if version is None:
                version = self.form.get("version")
            if version is not None:
                parts.append(version)
            raise Redirect, "/%s/json" % "/".join(parts)

        if version is None:
            if self.form.get('version'):
                version = self.form['version']
            else:
                l = self.store.get_latest_release(name, hidden=hidden)
                try:
                    version = l[0][1]
                except IndexError:
                    raise NotFound, 'no releases'
        info = self.store.get_package(name, version)
        if not info:
            raise NotFound, 'invalid name/version'
        return info, name, version

    def doap(self, name=None, version=None):
        '''Return DOAP rendering of a package.
        '''
        info, name, version = self._get_latest_pkg_info(name, version)

        root = cElementTree.Element('rdf:RDF', {
            'xmlns:rdf': "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
            'xmlns:foaf': "http://xmlns.com/foaf/0.1/",
            'xmlns': "http://usefulinc.com/ns/doap#"})
        SE = cElementTree.SubElement

        project = SE(root, 'Project')

        def write_element(parent, attr, element):
            value = info[attr]
            if not value or value == 'UNKNOWN':
                return
            element = SE(parent, element)
            element.text = value
            element.tail = '\n'

         # Not handled here: version, keywords
        for attr, element in [('name', 'name'),
                              ('summary', 'shortdesc'),
                              ('description', 'description'),
                              ('download_url', 'download-page')
                              ]:
              write_element(project, attr, element)

        url = info['home_page']
        if url and url != 'UNKNOWN':
            url = SE(project, 'homepage', {'rdf:resource': url})
            url.tail = '\n'

        person = 'maintainer'
        if not info[person]:
            person = 'author'
        if info[person]:
            maint = SE(project, 'maintainer')
            pelem = SE(maint, 'foaf:Person')
            write_element(pelem, person, 'foaf:name')
            email = info[person+'_email']
            if email and email != 'UNKNOWN':
                # sha1 requires ascii not unicode
                if isinstance(email, unicode): email = email.encode('utf8')
                obj = hashlib.sha1(email)
                email = binascii.b2a_hex(obj.digest())
                elem = SE(pelem, 'foaf:mbox_sha1sum')
                elem.text = email
            maint.tail = '\n'

        # Write version
        version = info['version']
        if version:
            release = SE(project, 'release')
            release.tail = '\n'
            velem = SE(release, 'Version')
            revision = SE(velem, 'revision')
            revision.text = version

        # write XML
        s = cStringIO.StringIO()
        s.write('<?xml version="1.0" encoding="UTF-8" ?>\n')
        cElementTree.ElementTree(root).write(s, 'utf-8')
        filename = '%s-%s.xml'%(name.encode('ascii', 'replace'),
            version.encode('ascii', 'replace'))

        self.handler.send_response(200, "OK")
        self.handler.set_content_type('text/xml; charset="UTF-8"')
        self.handler.send_header('Content-Disposition',
            'attachment; filename=%s'%filename)
        self.handler.end_headers()
        self.wfile.write(s.getvalue())

    def json(self, name=None, version=None):
        '''Return JSON rendering of a package.
        '''
        self.store.set_read_only()
        info, name, version = self._get_latest_pkg_info(name, version, hidden=None)

        package_releases = self.store.get_package_releases(name)
        releases = dict((release['version'], rpc.release_urls(self.store, release['name'], release['version'])) for release in package_releases)
        serial = self.store.changelog_last_serial() or 0

        d = {
            'info': rpc.release_data(self.store, name, version),
            'urls': rpc.release_urls(self.store, name, version),
            'releases': releases,
        }
        for url in d['urls']:
            url['upload_time'] = url['upload_time'].strftime('%Y-%m-%dT%H:%M:%S')
        for release, release_files in d['releases'].iteritems():
            for file in release_files:
                file['upload_time'] = file['upload_time'].strftime('%Y-%m-%dT%H:%M:%S')
                file['digests'] = {'md5': file['md5_digest'], 'sha256': file['sha256_digest']}
        self.handler.send_response(200, "OK")
        self.handler.set_content_type('application/json; charset="UTF-8"')
        self.handler.send_header('Content-Disposition', 'inline')
        self.handler.send_header("X-PYPI-LAST-SERIAL", str(serial))
        self.handler.send_header("Surrogate-Key", str("json pkg~%s" % safe_name(name).lower()))
        self.handler.send_header("Surrogate-Control", "max-age=86400")
        self.handler.send_header("Cache-Control", "max-age=600, public")
        self.handler.send_header("Access-Control-Allow-Origin", "*")
        self.handler.end_headers()
        # write the JSONP extra crap if necessary
        s = json.dumps(d, indent=4)
        callback = self.form.get('callback')
        if callback:
            s = '%s(%s)' % (callback, s)
        self.wfile.write(s)

    def display_pkginfo(self, name=None, version=None):
        '''Reconstruct and send a PKG-INFO metadata file.
        '''
        # XXX tarek need to add 1.2 support here
        #
        info, name, version = self._get_latest_pkg_info(name, version)
        if not info:
            return self.fail('No such package / version',
                heading='%s %s'%(name, version),
                content="I can't find the package / version you're requesting")

        content = cStringIO.StringIO()
        def w(s):
            if isinstance(s, unicode): s = s.encode('utf8')
            content.write(s)

        # We're up to PEP 314
        w("Metadata-Version: 1.1\n")

        # now the package info
        keys = info.keys()
        keys.sort()
        keypref = 'name version author author_email maintainer maintainer_email home_page download_url summary license description keywords platform'.split()
        for key in keypref:
            value = info[key]
            if not value:
                continue

            label = key.capitalize().replace('_', '-')

            if key == 'description':
                value = rfc822_escape(value)
            elif key.endswith('_email'):
                value = cgi.escape(value)
                value = value.replace('@', ' at ')
                value = value.replace('.', ' ')
            w('%s: %s\n'%(label, value))

        for col in ('requires', 'provides', 'obsoletes'):
            l = self.store.get_release_relationships(name, version, col)
            for entry in l:
                w('%s: %s\n' %(col.capitalize(), entry['specifier']))

        classifiers = self.store.get_release_classifiers(name, version)
        for c in classifiers:
            w('Classifier: %s\n' % (c['classifier'],))
        w('\n')

        # Not using self.success or page_head because we want
        # plain-text without all the html trappings.
        self.handler.send_response(200, "OK")
        self.handler.set_content_type('text/plain; charset=utf-8')
        self.handler.end_headers()
        s = content.getvalue()
        self.wfile.write(s)

    def release_nav(self):
        name = self.form.get('name')
        if not name:
            return ''

        # permission to do this?
        if not self.loggedin:
            return
        if not (self.store.has_role('Owner', name) or
                self.store.has_role('Admin', name) or
                self.store.has_role('Maintainer', name)):
            return ''

        # determine the version
        version = self.form.get('version')
        if not version:
            l = self.store.get_latest_release(name, hidden=False)
            try:
                version = l[-1][1]
            except IndexError:
                version = "(latest release)"
        if isinstance(version , list):
            version = version[0]

        un = urllib.quote_plus(name.encode('utf-8'))
        uv = urllib.quote_plus(version.encode('utf-8'))
        url = '%s?name=%s&amp;version=%s'%(self.url_path, un, uv)
        return '''<p class="release-nav">Package:
  <a href="%s?:action=role_form&amp;package_name=%s">roles</a> |
  <a href="%s?:action=pkg_edit&amp;name=%s">releases</a> |
  <a href="%s&amp;:action=display">view</a> |
  <a href="%s&amp;:action=files">files</a> |
  <a href="%s&amp;:action=display_pkginfo">PKG-INFO</a>
</p>'''%(self.url_path, un, self.url_path, un, url, url, url)

    def quote_plus(self, data):
        return urllib.quote_plus(data)

    def _load_release_info(self, name, version):
        '''Determine the information about a release of the named package.

        If version is specified then we return that version and also determine
        what the latest version of the package is.

        If the version is None then we return the latest version.
        '''
        # get the appropriate package info from the database
        if name is None:
            # check that we get one - and only one - name argument
            name = self.form.get('name', None)
            if name is None or isinstance(name, list):
                self.fail("Which package do you want to display?")

        using_latest = False
        if version is None:
            if self.form.get('version'):
                version = self.form['version']
            else:
                l = self.store.get_package_releases(name, hidden=False)
                if len(l) > 1:
                    raise MultipleReleases(releases=l)
                l = self.store.get_latest_release(name, hidden=False)
                try:
                    version = l[-1][1]
                except IndexError:
                    using_latest = True
                    version = "(latest release)"

        if not using_latest:
             l = self.store.get_package_releases(name, hidden=False)
             latest_version = self.store.get_latest_release(name, hidden=False)
             try:
                 latest_version = l[0][1]
             except:
                 latest_version = 'Unknown'
                 # for now silently fail, this simply means we were not able
                 # to determine what the latest release is for one reason or
                 # another
        else:
             latest_version = None

        info = self.store.get_package(name, version)
        if not info:
            raise NotFound
        return info, latest_version

    def display(self, name=None, version=None, ok_message=None,
            error_message=None):
        ''' Print up an entry
        '''
        try:
            info, latest_version = self._load_release_info(name, version)
        except MultipleReleases, e:
            return self.index(releases=e.releases)
        except NotFound:
            if name is None:
                # check that we get one - and only one - name argument
                name = self.form.get('name', None)
                if name is None:
                    raise NotFound
            # Try to locate the normalized name
            found = self.store.find_package(name)
            if not found or found[0] == name:
                raise
            realname = found[0]
            url = "%s/%s" % (self.config.url, realname)
            if version is None:
                if self.form.get('version'):
                    version = self.form['version']
            if version:
                url = url + "/" + version
            raise Redirect, url


        name = info['name']
        version = info['version']
        using_latest = latest_version==version

# RJ: disabled cheesecake because the (strange) errors were getting annoying
#        columns = 'name version author author_email maintainer maintainer_email home_page download_url summary license description description_html keywords platform cheesecake_installability_id cheesecake_documentation_id cheesecake_code_kwalitee_id'.split()
        columns = ('name version author author_email maintainer '
                   'maintainer_email home_page requires_python download_url '
                   'summary license description keywords '
                   'platform bugtrack_url').split()

        release = {'description_html': ''}
        bugtrack_url =''
        for column in columns:
            value = info[column]
            if not info[column]: continue
            if isinstance(value, basestring) and value.strip() in (
                    'UNKNOWN', '<p>UNKNOWN</p>'): continue

            if column in {"bugtrack_url", "home_page", "download_url"}:
                uri = uri_reference(value)
                if not uri.is_valid(require_scheme=True, require_authority=True):
                    continue
                if uri.scheme not in {"https", "http"}:
                    continue

            if column in ('name', 'version'): continue
            elif column.endswith('_email'):
                column = column[:column.find('_')]
                if release.has_key(column):
                    start = release[column]
                else:
                    start = ''
                value = value.replace('@', ' at ')
                value = value.replace('.', ' ')
                value = '%s <%s>'%(start, value)
            elif column.startswith('cheesecake_'):
                column = column[:-3]
                value = self.store.get_cheesecake_index(int(value))
            elif column == 'bugtrack_url':
                bugtrack_url = value

            value = info[column]
            release[column] = value

        if release.get("description"):
            # Render the project description
            description_html = readme_renderer.rst.render(release["description"])

            if description_html is None:
                description_html = readme_renderer.txt.render(release["description"])

            release["description_html"] = description_html

        roles = {}
        for role, user in self.store.get_package_roles(name):
            roles.setdefault(role, []).append(user)

        def values(col):
            l = self.store.get_release_relationships(name, version, col)
            return [x['specifier'] for x in l]

        categories = []
        is_py3k = False
        for c in self.store.get_release_classifiers(name, version):
            path = str2path(c['classifier'])
            pathstr = path2str(path)
            if pathstr.startswith('Programming Language :: Python :: 3'):
                is_py3k = True
            url = "%s?:action=browse&c=%s" % (self.url_path, c['trove_id'])
            categories.append(dict(
                name = c['classifier'],
                path = path,
                pathstr = pathstr,
                url = url,
                id = c['trove_id']))

        latest_version_url = '%s/%s/%s' % (self.config.url, name,
                                           latest_version)

        # New metadata
        requires_dist = self.store.get_package_requires_dist(name, version)
        provides_dist = self.store.get_package_provides_dist(name, version)
        obsoletes_dist = self.store.get_package_obsoletes_dist(name, version)
        project_url = self.store.get_package_project_url(name, version)
        requires_external = self.store.get_package_requires_external(name, version)

        docs = self.store.docs_url(name)
        files = self.store.list_files(name, version)

        # Download Counts from redis
        # We've disabled download counts because the statistics collection has
        # broken and somebody has yet to fix it. This will prevent people from
        # seeing messages that something has been downloaded zero times in the
        # last N.
        # try:
        #     download_counts = self.store.download_counts(name)
        # except redis.exceptions.ConnectionError as conn_fail:
        #     download_counts = False
        download_counts = False

        self.write_template('display.pt',
                            name=name, version=version, release=release,
                            description=release.get('summary') or name,
                            keywords=release.get('keywords', ''),
                            title=name + " " +version,
                            requires=values('requires'),
                            provides=values('provides'),
                            obsoletes=values('obsoletes'),
                            files=files,
                            docs=docs,
                            categories=categories,
                            is_py3k=is_py3k,
                            roles=roles,
                            newline_to_br=newline_to_br,
                            usinglatest=using_latest,
                            latestversion=latest_version,
                            latestversionurl=latest_version_url,
                            action=self.link_action(),
                            requires_dist=requires_dist,
                            provides_dist=provides_dist,
                            obsoletes_dist=obsoletes_dist,
                            requires_external=requires_external,
                            project_url=project_url,
                            download_counts=download_counts,
                            bugtrack_url=bugtrack_url,
                            requires_python=release.get('requires_python', ''))

    def index(self, nav_current='index', releases=None):
        ''' Print up an index page
        '''
        self.nav_current = nav_current
        if releases is None:
            data = dict(title="Index of Packages[Deprecated]")
            self.write_template('print_the_world.pt', **data)
        else:
            l = releases
            data = dict(title="Index of Packages", matches=l)
            if 'name' in self.form:
                data['name'] = self.form['name']
            self.write_template('index.pt', **data)

    STOPWORDS = set([
        "a", "and", "are", "as", "at", "be", "but", "by",
        "for", "if", "in", "into", "is", "it",
        "no", "not", "of", "on", "or", "such",
        "that", "the", "their", "then", "there", "these",
        "they", "this", "to", "was", "will",
    ])
    def search(self, nav_current='index'):
        ''' Search for the indicated term.

        Try name first, then summary then description. Collate a
        score for each package that matches.
        '''
        term = self.form.get('term', '')
        if isinstance(term, list):
            term = ' '.join(term)
        term = re.sub(r'[^\w\s\.\-]', '', term.strip().lower())
        terms = [t for t in term.split() if t not in self.STOPWORDS]
        terms = filter(None, terms)
        if not terms:
            raise FormError, 'You need to supply a search term'

        d = {}
        columns = [
            ('name', 4),      # doubled for exact (case-insensitive) match
            ('summary', 2),
            ('keywords', 2),
            ('description', 1),
            ('author', 1),
            ('maintainer', 1),
        ]


        # score all package/release versions
        # require that each term occurs at least once (AND search)
        for t in terms:
            d_new = {}
            for col, score in columns:
                spec = {'_pypi_hidden': False, col: t}
                for r in self.store.search_packages(spec):
                    key = (r['name'], r['version'])
                    if d:
                        # must find current score in d
                        if key not in d:
                            # not a candidate anymore
                            continue
                        else:
                            e = d[key]
                    else:
                        # may find score in d_new
                        e = d_new.get(key, [0, r])
                    if col == 'name' and safe_name(t).lower() == safe_name(r['name']).lower():
                        e[0] += score*2
                    else:
                        e[0] += score
                    d_new[key] = e
            d = d_new
            if not d:
                # no packages match
                break

        # record the max value of _pypi_ordering per package
        max_ordering = {}
        for score,r in d.values():
            old_max = max_ordering.get(r['name'], -1)
            max_ordering[r['name']] = max(old_max, r['_pypi_ordering'])

        # drop old releases
        for (name, version), (score, r) in d.items():
            if max_ordering[name] != r['_pypi_ordering']:
                del d[(name, version)]

        # now sort by score and name ordering
        l = []
        scores = {}
        for k,v in d.items():
            l.append((-v[0], k[0].lower(), v[1]))
            scores[k[0]] = v[0]

        if len(l) == 1:
            raise RedirectTemporary, "%s/%s/%s" % (self.config.url,l[0][-1]['name'],l[0][-1]['version'])

        # sort and pull out just the record
        l.sort()
        l = [e[-1] for e in l]

        self.write_template('index.pt', matches=l, scores=scores,
            title="Index of Packages Matching '%s'"%term)

    def csrf_check(self):
        '''Check that the required CSRF token is present in the form
        submission.
        '''
        if self.form.get('CSRFToken') != self.store.get_token(self.username):
            raise FormError, "Form Failure; reset form submission"

    @must_tls
    def submit_pkg_info(self):
        raise Gone(
            ("This API has been deprecated and removed from legacy PyPI in favor of "
             " using the APIs available in the new PyPI.org implementation of PyPI "
             "(located at https://pypi.org/). For more information about migrating your "
             "use of this API to PyPI.org, please see {}. For more information about "
             "the sunsetting of this API, please see "
             "https://mail.python.org/pipermail/distutils-sig/2017-June/030766.html").format(
                WAREHOUSE_UPLOAD_MIGRATION_URL,
             )
        )

    @must_tls
    def submit(self, parameters=None, response=True):
        raise Gone(
            ("This API has been deprecated and removed from legacy PyPI in "
             "favor of using the APIs available in the new PyPI.org "
             "implementation of PyPI (located at https://pypi.org/). For more "
             "information about migrating your use of this API to PyPI.org, "
             "please see {}. For more information about the sunsetting of "
             "this API, please see "
             "https://mail.python.org/pipermail/distutils-sig/2017-June/030766.html").format(
                WAREHOUSE_UPLOAD_MIGRATION_URL,
            )
        )

    def form_metadata(self, submitted_data=None):
        ''' Extract metadata from the form.
        '''
        if submitted_data is None:
            submitted_data = self.form
        data = {}
        for k in submitted_data:
            if k.startswith(':'): continue
            v = self.form[k]
            if k == '_pypi_hidden':
                v = v == '1'
            elif k in ('requires', 'provides', 'obsoletes',
                       'requires_dist', 'provides_dist',
                       'obsoletes_dist',
                       'requires_external', 'project_url'):
                if not isinstance(v, list):
                    v = [x.strip() for x in re.split('\s*[\r\n]\s*', v)]
                else:
                    v = [x.strip() for x in v]
                v = filter(None, v)
            elif isinstance(v, list):
                if k == 'classifiers':
                    v = [x.strip() for x in v]
                else:
                    v = ','.join([x.strip() for x in v])
            elif isinstance(v, FileUpload):
                continue
            else:
                v = v.strip()
            data[k.lower()] = v

        # make sure relationships are lists
        for name in ('requires', 'provides', 'obsoletes',
                     'requires_dist', 'provides_dist',
                     'obsoletes_dist',
                     'requires_external', 'project_url'):
            if data.has_key(name) and not isinstance(data[name],
                    types.ListType):
                data[name] = [data[name]]

        # make sure classifiers is a list
        if data.has_key('classifiers'):
            classifiers = data['classifiers']
            if not isinstance(classifiers, types.ListType):
                classifiers = [classifiers]
            data['classifiers'] = classifiers

        return data

    def verify(self):
        raise Gone(
            ("This API has been deprecated and removed from legacy PyPI in "
             "favor of using the APIs available in the new PyPI.org "
             "implementation of PyPI (located at https://pypi.org/). For more "
             "information about migrating your use of this API to PyPI.org, "
             "please see {}. For more information about the sunsetting of "
             "this API, please see "
             "https://mail.python.org/pipermail/distutils-sig/2017-June/030766.html").format(
                WAREHOUSE_UPLOAD_MIGRATION_URL,
            )
        )

    def pkg_edit(self):
        ''' Edit info about a bunch of packages at one go
        '''
        # make sure the user is identified
        if not self.authenticated:
            raise Unauthorised, \
                "You must be identified to edit package information"

        # this is used to render the form as well as edit it... UGH
        #self.csrf_check()

        if 'name' not in self.form:
            raise FormError("Invalid package name")

        name = self.form['name']
        editing = self.env['REQUEST_METHOD'] == "POST"

        if self.form.has_key('submit_remove'):
            return self.remove_pkg()

        if name.lower() in ('requirements.txt', 'rrequirements.txt'):
            raise Forbidden, "Package name '%s' invalid" % name

        # make sure the user has permission to do stuff
        if not (self.store.has_role('Owner', name) or
                self.store.has_role('Admin', name) or
                self.store.has_role('Maintainer', name)):
            raise Forbidden, \
                "You are not allowed to edit '%s' package information"%name

        if self.form.has_key('submit_autohide'):
            value = self.form.has_key('autohide')
            self.store.set_package_autohide(name, value)

        # look up the current info about the releases
        releases = list(self.store.get_package_releases(name))
        reldict = {}
        for release in releases:
            info = {}
            for k,v in release.items():
                info[k] = v
            reldict[info['version']] = info

        # see if we're editing (note that form keys don't get unquoted)
        for key in self.form.keys():
            if key.startswith('hid_'):
                ver = urllib.unquote(key[4:])
                info = reldict[ver]
                info['_pypi_hidden'] = self.form[key] == '1'
            elif key.startswith('sum_'):
                ver = urllib.unquote(key[4:])
                info = reldict[ver]
                info['summary'] = self.form[key]

        # update the database
        if editing:
            for version, info in reldict.items():
                self.store.store_package(name, version, info)
            self.store.changed()

        self.write_template('pkg_edit.pt', releases=releases, name=name,
            autohide=self.store.get_package_autohide(name),
            title="Package '%s' Editing"%name)

    def remove_pkg(self):
        ''' Remove a release or a whole package from the db.

            Only owner may remove an entire package - Maintainers may
            remove releases.
        '''
        # make sure the user is identified
        if not self.authenticated:
            raise Unauthorised, \
                "You must be identified to edit package information"

        self.csrf_check()

        # vars
        name = self.form['name']
        cn = cgi.escape(name)
        if self.form.has_key('version'):
            if isinstance(self.form['version'], type([])):
                version = [x for x in self.form['version']]
            else:
                version = [self.form['version']]
            cv = cgi.escape(', '.join(version))
            s = len(version)>1 and 's' or ''
            desc = 'release%s %s of project %s.'%(s, cv, cn)
        else:
            version = []
            desc = '<b>all</b> information about <b>and all releases of</b> project %s.'%cn

        # make sure the user has permission to do stuff
        if not (self.store.has_role('Owner', name) or
                self.store.has_role('Admin', name) or
                (version and self.store.has_role('Maintainer', name))):
            raise Forbidden, \
                "You are not allowed to edit '%s' package information"%name

        if self.form.has_key('submit_ok'):
            # ok, do it
            if version:
                for v in version:
                    self.store.remove_release(name, v)
                self.store.changed()
                self.ok_message='Release removed'
            else:
                self.store.remove_package(name)
                self.store.changed()
                self.ok_message='Package removed'
                return self.home()

        elif self.form.has_key('submit_cancel'):
            self.ok_message='Removal cancelled'

        else:
            message = '''You are about to remove the project %s from PyPI<br />
                This action <em>cannot be undone</em>!<br />
                <br />
                Consider that removing this may break people's system builds.<br />
                Are you <strong>sure</strong>?'''%desc

            fields = [
                {'name': ':action', 'value': 'remove_pkg'},
                {'name': 'name', 'value': name},
            ]
            for v in version:
                fields.append({'name': 'version', 'value': v})

            return self.write_template('dialog.pt', message=message,
                title='Confirm removal of %s'%desc, fields=fields)

        self.pkg_edit()

    # alias useful for the files ZPT page
    dist_file_types = store.dist_file_types
    dist_file_types_d = store.dist_file_types_d
    def files(self):
        '''List files and handle file submissions.
        '''
        name = version = None
        if self.form.has_key('name'):
            name = self.form['name']
        if self.form.has_key('version'):
            version = self.form['version']
        if not name or not version:
            self.fail(heading='Name and version are required',
                message='Name and version are required')
            return

        # if allowed, handle file upload
        maintainer = False
        if self.store.has_role('Maintainer', name) or \
                self.store.has_role('Admin', name) or \
                self.store.has_role('Owner', name):
            maintainer = True
            if self.form.has_key('submit_upload'):
                self.file_upload(response=False)

            elif (self.form.has_key('submit_remove') and
                    self.form.has_key('file-ids')):

                fids = self.form['file-ids']
                if isinstance(fids, list):
                    fids = [v for v in fids]
                else:
                    fids = [fids]

                for digest in fids:
                    file_info = self.store.get_file_info(digest)
                    try:
                        if self.store.has_role('Maintainer', file_info['name']) or \
                               self.store.has_role('Admin', file_info['name']) or \
                               self.store.has_role('Owner', file_info['name']):
                               self.store.remove_file(digest)
                        else:
                            raise Forbidden, \
                                "You are not allowed to edit '%s' package information"%file_info['name']
                    except KeyError:
                        return self.fail('No such files to remove', code=200)
                    else:
                        self.store.changed()

        self.write_template('files.pt', name=name, version=version,
            maintainer=maintainer, title="Files for %s %s"%(name, version))

    def pretty_size(self, size):
        n = 0
        while size > 1024:
            size /= 1024
            n += 1
        return '%d%sB'%(size, ['', 'K', 'M', 'G'][n])

    def show_md5(self):
        if not self.form.has_key('digest'):
            raise NotFound
        digest = self.form['digest']
        try:
            self.store.get_file_info(digest)
        except KeyError:
            # invalid MD5 digest - it's not in the database
            raise NotFound
        self.handler.send_response(200, 'OK')
        self.handler.set_content_type('text/plain; charset=utf-8')
        self.handler.end_headers()
        self.wfile.write(digest)

    @must_tls
    def file_upload(self, response=True, parameters=None):
        raise Gone(
            ("This API has been deprecated and removed from legacy PyPI in "
             "favor of using the APIs available in the new PyPI.org "
             "implementation of PyPI (located at https://pypi.org/). For more "
             "information about migrating your use of this API to PyPI.org, "
             "please see {}. For more information about the sunsetting of "
             "this API, please see "
             "https://mail.python.org/pipermail/distutils-sig/2017-June/030766.html").format(
                WAREHOUSE_UPLOAD_MIGRATION_URL,
            )
        )

    #
    # Documentation Upload
    #
    @must_tls
    def doc_upload(self):
        raise Gone(
            ("This API has been deprecated and removed from legacy PyPI in "
             "favor of using the APIs available in the new PyPI.org "
             "implementation of PyPI (located at https://pypi.org/). For more "
             "information about migrating your use of this API to PyPI.org, "
             "please see {}. For more information about the sunsetting of "
             "this API, please see "
             "https://mail.python.org/pipermail/distutils-sig/2017-June/030766.html").format(
                WAREHOUSE_UPLOAD_MIGRATION_URL,
            )
        )

    #
    # Reverse download for Google AppEngine
    #
    def gae_file(self):
        host = self.form['host']
        secret = self.form['secret']
        gae.transfer(host, secret, self.config.database_files_dir)
        self.handler.send_response(204, 'Initiated')
        self.handler.end_headers()

    #
    # classifiers listing
    #
    def list_classifiers(self):
        ''' Just return the list of classifiers.
        '''
        c = '\n'.join([c['classifier'] for c in self.store.get_classifiers()])
        self.handler.send_response(200, 'OK')
        self.handler.set_content_type('text/plain; charset=utf-8')
        self.handler.end_headers()
        self.wfile.write(c + '\n')

    #
    # User handling code (registration, password changing)
    #
    def user_form(self, openid_fields = (), username='', email='', openid=''):
        ''' Make the user authenticate before viewing the "register" form.
        '''
        if not self.authenticated:
            raise Unauthorised, 'You must authenticate'
        info = {'name': '', 'password': '', 'confirm': '', 'email': '',
                'openids': [], 'openid_fields': openid_fields,
                'openid': openid}
        user = self.store.get_user(self.username)
        info['new_user'] = False
        info['owns_packages'] = bool(self.store.user_packages(self.username, True))
        info['name'] = user['name']
        info['email'] = user['email']
        info['action'] = 'Update details'
        info['title'] = 'User profile'
        info['openids'] = self.store.get_openids(self.username)
        info['sshkeys'] = self.store.get_sshkeys(self.username)
        self.nav_current = 'user_form'
        self.write_template('register.pt', **info)

    def register_form(self):
        self.nav_current = 'register_form'
        info = {}
        info['title'] = 'User Registration'
        info['action'] = 'Register'
        self.write_template('register_gone.pt', **info)

    def user(self):
        ''' Register, update or validate a user.

            This interface handles one of three cases:
                1. completion of rego with One Time Key
                2. new user sending in name, password and email
                3. updating existing user details for currently authed user
        '''
        message = ''

        info = {}
        for param in 'name password email otk confirm'.split():
            v = self.form.get(param, '').strip()
            if v:
                info[param] = v

        # validate email syntax
        if info.has_key('email'):
            if not safe_email.match(info['email']):
                raise FormError, 'Email is invalid (ASCII only)'
            if '@' not in info['email'] or '.' not in info['email']:
                raise FormError, 'Email is invalid'
            domain = info['email'].split('@')[1]
            if domain in DOMAIN_BLACKLIST:
                raise FormError, 'Disposable email addresses not allowed'

        # email requirement check
        if 'email' not in info and 'otk' not in info:
            raise FormError, "Clearing the email address is not allowed"

        if info.has_key('otk'):
            # finish off rego
            if self.store.get_otk(info['otk']):
                response = 'Error: One Time Key invalid'
            elif self.form.has_key('agree_shown'):
                # user has posted the form with the usage agreement
                if not self.form.has_key('agree'):
                    self.fail('You need to confirm the usage agreement.',
                              heading='User registration')
                    return
                # OK, delete the key
                user = self.store.get_user_by_otk(info['otk'])
                self.store.delete_otk(info['otk'])
                self.store.activate_user(user)
                self.write_template('message.pt', title='Registration complete',
                                    message='You are now registered.',
                                    url='%s?:action=login_form' % self.url_path,
                                    url_text='Proceed to login')
                return
            else:
                # user has clicked the link in the email -- show agreement form
                user = self.store.get_user_by_otk(info['otk'])
                self.write_template('confirm.pt', title='Confirm registration',
                                    otk=info['otk'], user=user)
                return
        elif self.username is None:
            for param in 'name email'.split():
                if not info.has_key(param):
                    raise FormError, '%s is required'%param

            if 'password' not in info or 'confirm' not in info:
                raise FormError, 'password and confirm are required'
            else:
                claimed_id = None
                msg = self._verify_new_password(info['password'],
                    info['confirm'])
                if msg:
                    return self.fail(msg, heading='Users')

            # validate a complete set of stuff
            # new user, create entry and email otk
            name = info['name']
            if not safe_username.match(name):
                raise FormError, 'Username is invalid (ASCII alphanum,.,_ only)'
            if self.store.has_user(name):
                self.fail('user "%s" already exists' % name,
                    heading='User registration')
                return
            olduser = self.store.get_user_by_email(info['email'])
            if olduser:
                raise FormError, 'You have already registered as user '+olduser['name']

            info['otk'] = self.store.store_user(name, info['password'], info['email'])
            if claimed_id:
                self.store.associate_openid(name, claimed_id)
            info['url'] = self.config.url
            info['admin'] = self.config.adminemail
            self.send_email(info['email'], rego_message%info)
            response = 'Registration OK'
            message = ('You should receive a confirmation email to %s shortly. '
                       'To complete the registration process, visit the link '
                       'indicated in the email.') % info['email']

        else:
            self.csrf_check()

            # update details
            user = self.store.get_user(self.username)
            password = info.get('password', '').strip()
            confirm = info.get('confirm', '').strip()
            if not password:
                # no password entered - leave it alone
                password = None
            else:
                # make sure the confirm matches
                msg = self._verify_new_password(password, confirm, user)
                if msg:
                    return self.fail(msg, heading='User profile')
            email = info.get('email', user['email'])
            self.store.store_user(self.username, password, email)
            response = 'Details updated OK'

        self.write_template('message.pt', title=response, message=message)

    def addkey(self):
        if not self.authenticated:
            raise Unauthorised

        if "key" not in self.form:
            raise FormError, "missing key"

        self.csrf_check()

        key = self.form['key'].splitlines()
        for line in key[1:]:
            if line.strip():
                raise FormError, "Invalid key format: multiple lines"
        key = key[0].strip()
        if not any(pfx for pfx in 'ssh-dss ssh-rsa ecdsa-sha2-nistp'
                if key.startswith(pfx)):
            raise FormError("Invalid key format: does not start with ssh-dss, "
                "ssh-rsa or ecdsa-sha2-nistp*")
        self.store.add_sshkey(self.username, key)
        self.store.commit()
        return self.register_form()

    def delkey(self):
        if not self.authenticated:
            raise Unauthorised

        if "id" not in self.form:
            raise FormError, "missing parameter"

        self.csrf_check()

        try:
            id = int(self.form["id"])
        except:
            raise FormError, "invalid ID"
        for key in self.store.get_sshkeys(self.username):
            if key['id'] == id:
                break
        else:
            raise Unauthorised, "not your key"
        self.store.delete_sshkey(id)
        self.store.commit()
        return self.register_form()

    def password_reset(self):
        """Send a password reset email to the user attached to the address
        nominated.

        This is a legacy interface used by distutils which supplies an email
        address.
        """
        email = self.form.get('email', '').strip()
        user = self.store.get_user_by_email(email)
        if not user:
            return self.fail('email address unknown to me')

        # check for existing registration-confirmation OTK
        if self.store.get_otk(user['name']):
            info = {'otk': self.store.get_otk(user['name']),
                'url': self.config.url, 'admin': self.config.adminemail,
                'email': user['email'], 'name':user['name']}
            self.send_email(info['email'], rego_message%info)
            self.write_template('message.pt', title="Resending registration key",
                message='Email with registration key resent')

        # generate a reset OTK and mail the link - force link to be HTTPS
        url = self.config.url
        if url[:4] == 'http':
            url = 'https' + url[4:]
        info = dict(name=user['name'], url=url, email=user['email'],
            otk=self._gen_reset_otk(user))
        info['admin'] = self.config.adminemail
        self.send_email(user['email'], password_change_message % info)
        self.write_template('message.pt', title="Request password reset",
            message='Email sent to confirm password change')

    def forgotten_password_form(self):
        ''' Enable the user to reset their password.

        This is the first leg of a password reset and requires the user
        identify themselves somehow by supplying their username or email
        address.
        '''
        self.write_template("password_reset.pt",
            title="Request password reset")

    def forgotten_password(self):
        '''Accept a user's submission of username and send a
        reset email if it's valid.
        '''
        name = self.form.get('name', '').strip()
        if not name:
            self.write_template("password_reset.pt",
                title="Request password reset", retry=True)

        user = self.store.get_user(name)
        # typically other systems would not indicate the username is invalid
        # but in PyPI's case the username list is public so this is more
        # user-friendly with no security penalty
        if not user:
            self.fail('user "%s" unknown to me' % name)
            return

        # existing registration OTK?
        if self.store.get_otk(user['name']):
            info = dict(
                otk=self.store.get_otk(user['name']),
                url=self.config.url,
                admin=self.config.adminemail,
                email=user['email'],
                name=user['name'],
            )
            self.send_email(info['email'], rego_message % info)
            return self.write_template('message.pt',
                title="Resending registration key",
                message='Email with registration key resent')

        # generate a reset OTK and mail the link
        info = dict(name=user['name'], url=self.config.url,
            email=user['email'], otk=self._gen_reset_otk(user))
        info['admin'] = self.config.adminemail
        self.send_email(info['email'], password_change_message % info)
        self.write_template('message.pt', title="Request password reset",
            message='Email sent to confirm password change')

    def _gen_reset_otk(self, user):
        # generate the reset key and sign it
        reset_signer = itsdangerous.URLSafeTimedSerializer(
            self.config.reset_secret, 'password-recovery')

        # we include a snip of the current password hash so that the OTK can't
        # be used again once the password is changed. And hash it to be extra
        # obscure
        return reset_signer.dumps((user['name'], user['password'][-4:]))

    def _decode_reset_otk(self, otk):
        reset_signer = itsdangerous.URLSafeTimedSerializer(
            self.config.reset_secret, 'password-recovery')
        try:
            # we allow 6 hours
            name, pwfrag = reset_signer.loads(otk, max_age=6*60*60)
        except itsdangerous.BadData:
            return None
        user = self.store.get_user(name)
        if pwfrag == user['password'][-4:]:
            return user
        return None

    def pw_reset(self):
        '''The user has clicked the reset link in the email we sent them.

        Validate the OTK we are given and display a form for them to set their
        new password.
        '''
        otk = self.form.get('otk', '').strip()
        user = self._decode_reset_otk(otk)
        if not user:
            self.fail('invalid password reset token')
            return
        self.write_template('password_reset_change.pt', otk=otk,
            title="Password reset")

    def pw_reset_change(self):
        '''The final leg in the password reset sequence: accept the new
        password.'''
        otk = self.form.get('otk', '').strip()
        user = self._decode_reset_otk(otk)
        if not user:
            self.fail('invalid password reset token')
            return

        pw = self.form.get('password', '').strip()
        confirm = self.form.get('confirm', '').strip()

        msg = self._verify_new_password(pw, confirm, user)
        if msg:
            return self.write_template('password_reset_change.pt',
                title="Password reset", otk=otk, retry=msg)

        self.store.store_user(user['name'], pw, user['email'], None)
        self.write_template('message.pt', title="Password reset",
            message='Password has been reset')

    def _verify_new_password(self, pw, confirm, user=None):
        '''Verify that the new password is good.

        The messages here may be returned as plain text so wrap at 80 columns if
        necessary.

        Returns a reason string if the verification fails.
        '''
        # TODO consider strengthening this using information in:
        # https://github.com/fedora-infra/fas/blob/develop/fas/validators.py#L237
        if user and self.config.passlib.verify(pw, user['password']):
            return 'Please ensure the new password is not the same as the old.'

        if user and pw == user['name']:
            return 'Please make your password harder to guess.'

        if pw != confirm:
            return "Please check you've entered the same password in "\
                "both fields."

        if len(pw) < 8:
            return "Please make your password at least 8 characters long."

        if len(pw) < 16 and (pw.isdigit() or pw.isalpha() or pw.isupper()
                or pw.islower()):
            return 'Please use 16 or more characters, or a mix of ' \
                   'different-case letters and numbers '\
                   'in your password.'

        return ''

    def delete_user(self):
        if not self.authenticated:
            raise Unauthorised

        if self.form.has_key('submit_ok'):
            self.csrf_check()
            # ok, do it
            self.store.delete_user(self.username)
            self.authenticated = self.loggedin = False
            self.username = self.usercookie = None
            return self.home()
        elif self.form.has_key('submit_cancel'):
            self.ok_message='Deletion cancelled'
            return self.home()
        else:
            message = '''You are about to delete the %s account<br />
                This action <em>cannot be undone</em>!<br />
                Are you <strong>sure</strong>?'''%self.username

            fields = [
                {'name': ':action', 'value': 'delete_user'},
            ]
            return self.write_template('dialog.pt', message=message,
                title='Confirm account deletion', fields=fields)

    def send_email(self, recipient, message):
        ''' Send an administrative email to the recipient
        '''
        smtp = smtplib.SMTP(self.config.smtp_hostname)
        if self.config.smtp_starttls:
            smtp.starttls()
        if self.config.smtp_auth:
            smtp.login(self.config.smtp_login, self.config.smtp_password)
        smtp.sendmail(self.config.adminemail, recipient, message)

    def packageURL(self, name, version):
        ''' return a URL for the link to display a particular package
        '''
        return self.store.package_url(self.url_path, name, version)

    def packageLink(self, name, version):
        ''' return a link to display a particular package
        '''
        if not isinstance(name, unicode): name = name.decode('utf-8')
        if not isinstance(version, unicode): version = version.decode('utf-8')
        url = self.packageURL(name, version)
        name = cgi.escape(name)
        version = cgi.escape(version)
        return u'<a href="%s">%s&nbsp;%s</a>'%(url, name, version)

    def mirrors(self):
        ''' display the list of mirrors
        '''
        options = {'title': 'PyPI Mirrors'}
        self.write_template('mirrors.pt', **options)

    def security(self):
        options = {'title': 'PyPI Security'}
        self.write_template('security.pt', **options)

    def tos(self):
        options = {'title': 'PyPI Terms of Service'}
        self.write_template('tos.pt', **options)

    def current_serial(self):
        # Provide an endpoint for quickly determining the current serial
        self.handler.send_response(200, 'OK')
        self.handler.set_content_type('text/plain')
        self.handler.end_headers()
        serial = self.store.changelog_last_serial() or 0
        self.wfile.write(str(serial))

    def daytime(self):
        # Mirrors are supposed to provide /last-modified,
        # but it doesn't make sense to do so for the master server
        '''display the current server time.
        '''
        self.handler.send_response(200, 'OK')
        self.handler.set_content_type('text/plain')
        self.handler.end_headers()
        self.wfile.write(time.strftime("%Y%m%dT%H:%M:%S\n", time.gmtime(time.time())))

    def get_providers(self):
        res = []
        for r in providers:
            r = Provider(*r)
            r.login = "/openid_login?provider=%s" % (r.name,)
            r.claim = "/openid_claim?provider=%s" % (r.name,)
            res.append(r)
        return res

    #
    # Google Login
    #

    def google_login(self):
        self.handler.set_status('200 OK')
        result = authomatic.login(PyPIAdapter(self.env, self.config, self.handler, self.form), 'google',
                                  return_url=self.config.baseurl+'/google_login',)
        if result:
            if result.user:
                content = result.user.data
                payload = parse(content['id_token']).payload
                result_openid_id = payload.get('openid_id', None)
                result_sub = payload.get('sub', None)
                user = None
                if result_sub:
                    found_user = self.store.get_user_by_openid_sub(result_sub)
                    if found_user:
                        user = found_user
                if user is None and result_openid_id:
                    found_user = self.store.get_user_by_openid(result_openid_id)
                    if found_user:
                        user = found_user
                        self.store.migrate_to_openid_sub(user['name'], result_openid_id, result_sub)
                        self.store.commit()
                if user:
                    self.username = user['name']
                    self.loggedin = self.authenticated = True
                    self.usercookie = self.store.create_cookie(self.username)
                    self.store.get_token(self.username)
                    self.store.commit()
                    self.statsd.incr('google_authentication.login')
                    self.dogstatsd.increment('authentication.complete', tags=['method:google_auth'])
                    self.home()
                else:
                    self.dogstatsd.increment('authentication.failure', tags=['method:google_auth'])
                    return self.fail("No PyPI user found associated with that Google Account, Associating new accounts has been deprecated.", code=400)


        self.handler.end_headers()
