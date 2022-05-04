"""
RStudio Connect API client and utility functions
"""

import time
from _ssl import SSLError

from .http_support import HTTPResponse, HTTPServer, append_to_path, CookieJar
from .log import logger
from .models import AppModes
from six import text_type
from .metadata import ServerStore, AppStore
from .actions import test_server, test_api_key, _default_title, _make_deployment_name, cli_feedback
from .bundle import make_html_bundle
from main import _deploy_bundle


class RSConnectException(Exception):
    def __init__(self, message, cause=None):
        super(RSConnectException, self).__init__(message)
        self.message = message
        self.cause = cause


class RSConnectServer(object):
    """
    A simple class to encapsulate the information needed to interact with an
    instance of the Connect server.
    """

    def __init__(self, url, api_key, insecure=False, ca_data=None):
        self.url = url
        self.api_key = api_key
        self.insecure = insecure
        self.ca_data = ca_data
        # This is specifically not None.
        self.cookie_jar = CookieJar()

    def handle_bad_response(self, response):
        if isinstance(response, HTTPResponse):
            if response.exception:
                raise RSConnectException(
                    "Exception trying to connect to %s - %s" % (self.url, response.exception), cause=response.exception
                )
            # Sometimes an ISP will respond to an unknown server name by returning a friendly
            # search page so trap that since we know we're expecting JSON from Connect.  This
            # also catches all error conditions which we will report as "not running Connect".
            else:
                if response.json_data and "error" in response.json_data:
                    error = "The Connect server reported an error: %s" % response.json_data["error"]
                    raise RSConnectException(error)
                if response.status < 200 or response.status > 299:
                    raise RSConnectException(
                        "Received an unexpected response from RStudio Connect: %s %s"
                        % (response.status, response.reason)
                    )


class RSConnect(HTTPServer):
    def __init__(self, server, cookies=None, timeout=30):
        if cookies is None:
            cookies = server.cookie_jar
        super(RSConnect, self).__init__(
            append_to_path(server.url, "__api__"),
            server.insecure,
            server.ca_data,
            cookies,
            timeout,
        )
        self._server = server

        if server.api_key:
            self.key_authorization(server.api_key)

    def _tweak_response(self, response):
        return (
            response.json_data
            if response.status and response.status == 200 and response.json_data is not None
            else response
        )

    def me(self):
        return self.get("me")

    def server_settings(self):
        return self.get("server_settings")

    def python_settings(self):
        return self.get("v1/server_settings/python")

    def app_search(self, filters):
        return self.get("applications", query_params=filters)

    def app_create(self, name):
        return self.post("applications", body={"name": name})

    def app_get(self, app_id):
        return self.get("applications/%s" % app_id)

    def app_upload(self, app_id, tarball):
        return self.post("applications/%s/upload" % app_id, body=tarball)

    def app_update(self, app_id, updates):
        return self.post("applications/%s" % app_id, body=updates)

    def app_add_environment_vars(self, app_guid, env_vars):
        env_body = [dict(name=kv[0], value=kv[1]) for kv in env_vars]
        return self.patch("v1/content/%s/environment" % app_guid, body=env_body)

    def app_deploy(self, app_id, bundle_id=None):
        return self.post("applications/%s/deploy" % app_id, body={"bundle": bundle_id})

    def app_publish(self, app_id, access):
        return self.post(
            "applications/%s" % app_id,
            body={"access_type": access, "id": app_id, "needs_config": False},
        )

    def app_config(self, app_id):
        return self.get("applications/%s/config" % app_id)

    def bundle_download(self, content_guid, bundle_id):
        response = self.get("v1/content/%s/bundles/%s/download" % (content_guid, bundle_id), decode_response=False)
        self._server.handle_bad_response(response)
        return response

    def content_search(self):
        response = self.get("v1/content")
        self._server.handle_bad_response(response)
        return response

    def content_get(self, content_guid):
        response = self.get("v1/content/%s" % content_guid)
        self._server.handle_bad_response(response)
        return response

    def content_build(self, content_guid, bundle_id=None):
        response = self.post("v1/content/%s/build" % content_guid, body={"bundle_id": bundle_id})
        self._server.handle_bad_response(response)
        return response

    def task_get(self, task_id, first_status=None):
        params = None
        if first_status is not None:
            params = {"first_status": first_status}
        response = self.get("tasks/%s" % task_id, query_params=params)
        self._server.handle_bad_response(response)
        return response

    def deploy(self, app_id, app_name, app_title, title_is_default, tarball, env_vars=None):
        if app_id is None:
            # create an app if id is not provided
            app = self.app_create(app_name)
            self._server.handle_bad_response(app)
            app_id = app["id"]

            # Force the title to update.
            title_is_default = False
        else:
            # assume app exists. if it was deleted then Connect will
            # raise an error
            app = self.app_get(app_id)
            self._server.handle_bad_response(app)

        app_guid = app["guid"]
        if env_vars:
            result = self.app_add_environment_vars(app_guid, list(env_vars.items()))
            self._server.handle_bad_response(result)

        if app["title"] != app_title and not title_is_default:
            self._server.handle_bad_response(self.app_update(app_id, {"title": app_title}))
            app["title"] = app_title

        app_bundle = self.app_upload(app_id, tarball)

        self._server.handle_bad_response(app_bundle)

        task = self.app_deploy(app_id, app_bundle["id"])

        self._server.handle_bad_response(task)

        return {
            "task_id": task["id"],
            "app_id": app_id,
            "app_guid": app["guid"],
            "app_url": app["url"],
            "title": app["title"],
        }

    def download_bundle(self, content_guid, bundle_id):
        results = self.bundle_download(content_guid, bundle_id)
        self._server.handle_bad_response(results)
        return results

    def search_content(self):
        results = self.content_search()
        self._server.handle_bad_response(results)
        return results

    def get_content(self, content_guid):
        results = self.content_get(content_guid)
        self._server.handle_bad_response(results)
        return results

    def wait_for_task(
        self, task_id, log_callback, abort_func=lambda: False, timeout=None, poll_wait=0.5, raise_on_error=True
    ):

        last_status = None
        ending = time.time() + timeout if timeout else 999999999999

        if log_callback is None:
            log_lines = []
            log_callback = log_lines.append
        else:
            log_lines = None

        sleep_duration = 0.5
        time_slept = 0
        while True:
            if time.time() >= ending:
                raise RSConnectException("Task timed out after %d seconds" % timeout)
            elif abort_func():
                raise RSConnectException("Task aborted.")

            # we continue the loop so that we can re-check abort_func() in case there was an interrupt (^C),
            # otherwise the user would have to wait a full poll_wait cycle before the program would exit.
            if time_slept <= poll_wait:
                time_slept += sleep_duration
                time.sleep(sleep_duration)
                continue
            else:
                time_slept = 0
                task_status = self.task_get(task_id, last_status)
                self._server.handle_bad_response(task_status)
                last_status = self.output_task_log(task_status, last_status, log_callback)
                if task_status["finished"]:
                    result = task_status.get("result")
                    if isinstance(result, dict):
                        data = result.get("data", "")
                        type = result.get("type", "")
                        if data or type:
                            log_callback("%s (%s)" % (data, type))

                    err = task_status.get("error")
                    if err:
                        log_callback("Error from Connect server: " + err)

                    exit_code = task_status["code"]
                    if exit_code != 0:
                        exit_status = "Task exited with status %d." % exit_code
                        if raise_on_error:
                            raise RSConnectException(exit_status)
                        else:
                            log_callback("Task failed. %s" % exit_status)
                    return log_lines, task_status

    @staticmethod
    def output_task_log(task_status, last_status, log_callback):
        """Pipe any new output through the log_callback.

        Returns an updated last_status which should be passed into
        the next call to output_task_log.

        Raises RSConnectException on task failure.
        """
        new_last_status = last_status
        if task_status["last_status"] != last_status:
            for line in task_status["status"]:
                log_callback(line)
            new_last_status = task_status["last_status"]

        return new_last_status


class RSConnectExecutor:
    def __init__(self, *args, **kwargs) -> None:
        print(kwargs)
        self.d = kwargs

    def validate_server(self, *args, **kwargs):
        """
        Validate that the user gave us enough information to talk to a Connect server.

        :param name: the nickname, if any, specified by the user.
        :param url: the URL, if any, specified by the user.
        :param api_key: the API key, if any, specified by the user.
        :param insecure: a flag noting whether TLS host/validation should be skipped.
        :param ca_cert: the name of a CA certs file containing certificates to use.
        :param api_key_is_required: a flag that notes whether the API key is required or may
        be omitted.
        """
        name = self.d['name']
        url = self.d['url']
        api_key = self.d['api_key']
        insecure = self.d['insecure']
        ca_cert = self.d['ca_cert']
        api_key_is_required = self.d['api_key_is_required']

        server_store = ServerStore()
        
        ca_data = ca_cert and text_type(ca_cert.read())

        if name and url:
            raise RSConnectException("You must specify only one of -n/--name or -s/--server, not both.")

        real_server, api_key, insecure, ca_data, from_store = server_store.resolve(name, url, api_key, insecure, ca_data)

        # This can happen if the user specifies neither --name or --server and there's not
        # a single default to go with.
        if not real_server:
            raise RSConnectException("You must specify one of -n/--name or -s/--server.")

        connect_server = RSConnectServer(real_server, None, insecure, ca_data)

        # If our info came from the command line, make sure the URL really works.
        if not from_store:
            connect_server, _ = test_server(connect_server)

        connect_server.api_key = api_key

        if not connect_server.api_key:
            if api_key_is_required:
                raise RSConnectException('An API key must be specified for "%s".' % connect_server.url)
            return connect_server

        # If our info came from the command line, make sure the key really works.
        if not from_store:
            _ = test_api_key(connect_server)

        self.d['connect_server'] = connect_server

        return self

    def make_bundle(self, *args, **kwargs):
        path = self.d['path']
        app_id = self.d['app_id']
        connect_server = self.d['connect_server']
        entrypoint = self.d['entrypoint']
        extra_files = self.d['extra_files']
        excludes = self.d['excludes']
        title = self.d['title']

        self.d['app_store'] = AppStore(path)
        self.d['default_title'] = not bool(title)
        self.d['title'] = title or _default_title(path)
        self.d['deployment_name'] = _make_deployment_name(connect_server, self.d['title'], app_id is None)

        with cli_feedback("Creating deployment bundle"):
            try:
                bundle = make_html_bundle(path, entrypoint, extra_files, excludes)
            except IOError as error:
                msg = "Unable to include the file %s in the bundle: %s" % (
                    error.filename,
                    error.args[1],
                )
                raise RSConnectException(msg)
        
        self.d['bundle'] = bundle

        return self

    def deploy_bundle(self, *args, **kwargs):        
        _deploy_bundle(
            self.d['connect_server'],
            self.d['app_store'],
            self.d['path'],
            self.d['app_id'],
            self.d['app_mode'],
            self.d['deployment_name'],
            self.d['title'],
            self.d['default_title'],
            self.d['bundle'],
            self.d['env_vars'],
        )

        return self

    def make_manifest(self, *args, **kwargs):
        pass

    def validate_app_mode(self, *args, **kwargs):
        connect_server = self.d['connect_server']
        app_store = self.d['app_store']
        new = self.d['new']
        app_id = self.d['app_id']
        default_app_mode = kwargs['default_app_mode']
        self.d | kwargs

        if new and app_id:
            raise RSConnectException("Specify either a new deploy or an app ID but not both.")

        app_mode = default_app_mode
        existing_app_mode = None
        if not new:
            if app_id is None:
                # Possible redeployment - check for saved metadata.
                # Use the saved app information unless overridden by the user.
                app_id, existing_app_mode = app_store.resolve(connect_server.url, app_id, app_mode)
                logger.debug("Using app mode from app %s: %s" % (app_id, app_mode))
            elif app_id is not None:
                # Don't read app metadata if app-id is specified. Instead, we need
                # to get this from Connect.
                app = get_app_info(connect_server, app_id)
                existing_app_mode = AppModes.get_by_ordinal(app.get("app_mode", 0), True)
            if existing_app_mode and app_mode != existing_app_mode:
                msg = (
                    "Deploying with mode '%s',\n"
                    + "but the existing deployment has mode '%s'.\n"
                    + "Use the --new option to create a new deployment of the desired type."
                ) % (app_mode.desc(), existing_app_mode.desc())
                raise RSConnectException(msg)    
        
        self.d['app_mode'] = app_mode
        return self


def verify_server(connect_server):
    """
    Verify that the given server information represents a Connect instance that is
    reachable, active and appears to be actually running RStudio Connect.  If the
    check is successful, the server settings for the Connect server is returned.

    :param connect_server: the Connect server information.
    :return: the server settings from the Connect server.
    """
    try:
        with RSConnect(connect_server) as client:
            result = client.server_settings()
            connect_server.handle_bad_response(result)
            return result
    except SSLError as ssl_error:
        raise RSConnectException("There is an SSL/TLS configuration problem: %s" % ssl_error)


def verify_api_key(connect_server):
    """
    Verify that an API Key may be used to authenticate with the given RStudio Connect server.
    If the API key verifies, we return the username of the associated user.

    :param connect_server: the Connect server information, including the API key to test.
    :return: the username of the user to whom the API key belongs.
    """
    with RSConnect(connect_server) as client:
        result = client.me()
        if isinstance(result, HTTPResponse):
            if result.json_data and "code" in result.json_data and result.json_data["code"] == 30:
                raise RSConnectException("The specified API key is not valid.")
            raise RSConnectException("Could not verify the API key: %s %s" % (result.status, result.reason))
        return result["username"]


def get_python_info(connect_server):
    """
    Return information about versions of Python that are installed on the indicated
    Connect server.

    :param connect_server: the Connect server information.
    :return: the Python installation information from Connect.
    """
    with RSConnect(connect_server) as client:
        result = client.python_settings()
        connect_server.handle_bad_response(result)
        return result


def get_app_info(connect_server, app_id):
    """
    Return information about an application that has been created in Connect.

    :param connect_server: the Connect server information.
    :param app_id: the ID (numeric or GUID) of the application to get info for.
    :return: the Python installation information from Connect.
    """
    with RSConnect(connect_server) as client:
        result = client.app_get(app_id)
        connect_server.handle_bad_response(result)
        return result


def get_app_config(connect_server, app_id):
    """
    Return the configuration information for an application that has been created
    in Connect.

    :param connect_server: the Connect server information.
    :param app_id: the ID (numeric or GUID) of the application to get the info for.
    :return: the Python installation information from Connect.
    """
    with RSConnect(connect_server) as client:
        result = client.app_config(app_id)
        connect_server.handle_bad_response(result)
        return result


def do_bundle_deploy(connect_server, app_id, name, title, title_is_default, bundle, env_vars):
    """
    Deploys the specified bundle.

    :param connect_server: the Connect server information.
    :param app_id: the ID of the app to deploy, if this is a redeploy.
    :param name: the name for the deploy.
    :param title: the title for the deploy.
    :param title_is_default: a flag noting whether the title carries a defaulted value.
    :param bundle: the bundle to deploy.
    :param env_vars: list of NAME=VALUE pairs for the app environment
    :return: application information about the deploy.  This includes the ID of the
    task that may be queried for deployment progress.
    """
    with RSConnect(connect_server, timeout=120) as client:
        result = client.deploy(app_id, name, title, title_is_default, bundle, env_vars)
        connect_server.handle_bad_response(result)
        return result


def emit_task_log(
    connect_server,
    app_id,
    task_id,
    log_callback,
    abort_func=lambda: False,
    timeout=None,
    poll_wait=0.5,
    raise_on_error=True,
):
    """
    Helper for spooling the deployment log for an app.

    :param connect_server: the Connect server information.
    :param app_id: the ID of the app that was deployed.
    :param task_id: the ID of the task that is tracking the deployment of the app..
    :param log_callback: the callback to use to write the log to.  If this is None
    (the default) the lines from the deployment log will be returned as a sequence.
    If a log callback is provided, then None will be returned for the log lines part
    of the return tuple.
    :param timeout: an optional timeout for the wait operation.
    :param poll_wait: how long to wait between polls of the task api for status/logs
    :param raise_on_error: whether to raise an exception when a task is failed, otherwise we
    return the task_result so we can record the exit code.
    :return: the ultimate URL where the deployed app may be accessed and the sequence
    of log lines.  The log lines value will be None if a log callback was provided.
    """
    with RSConnect(connect_server) as client:
        result = client.wait_for_task(task_id, log_callback, abort_func, timeout, poll_wait, raise_on_error)
        connect_server.handle_bad_response(result)
        app_config = client.app_config(app_id)
        connect_server.handle_bad_response(app_config)
        app_url = app_config.get("config_url")
        return (app_url, *result)


def retrieve_matching_apps(connect_server, filters=None, limit=None, mapping_function=None):
    """
    Retrieves all the app names that start with the given default name.  The main
    point for this function is that it handles all the necessary paging logic.

    If a mapping function is provided, it must be a callable that accepts 2
    arguments.  The first will be an `RSConnect` client, in the event extra calls
    per app are required.  The second will be the current app.  If the function
    returns None, then the app will be discarded and not appear in the result.

    :param connect_server: the Connect server information.
    :param filters: the filters to use for isolating the set of desired apps.
    :param limit: the maximum number of apps to retrieve.  If this is None,
    then all matching apps are returned.
    :param mapping_function: an optional function that may transform or filter
    each app to return to something the caller wants.
    :return: the list of existing names that start with the proposed one.
    """
    page_size = 100
    result = []
    search_filters = filters.copy() if filters else {}
    search_filters["count"] = min(limit, page_size) if limit else page_size
    total_returned = 0
    maximum = limit
    finished = False

    with RSConnect(connect_server) as client:
        while not finished:
            response = client.app_search(search_filters)
            connect_server.handle_bad_response(response)

            if not maximum:
                maximum = response["total"]
            else:
                maximum = min(maximum, response["total"])

            applications = response["applications"]
            returned = response["count"]
            delta = maximum - (total_returned + returned)
            # If more came back than we need, drop the rest.
            if delta < 0:
                applications = applications[: abs(delta)]
            total_returned = total_returned + len(applications)

            if mapping_function:
                applications = [mapping_function(client, app) for app in applications]
                # Now filter out the None values that represent the apps the
                # function told us to drop.
                applications = [app for app in applications if app is not None]

            result.extend(applications)

            if total_returned < maximum:
                search_filters = {
                    "start": total_returned,
                    "count": page_size,
                    "cont": response["continuation"],
                }
            else:
                finished = True

    return result


def override_title_search(connect_server, app_id, app_title):
    """
    Returns a list of abbreviated app data that contains apps with a title
    that matches the given one and/or the specific app noted by its ID.

    :param connect_server: the Connect server information.
    :param app_id: the ID of a specific app to look for, if any.
    :param app_title: the title to search for.
    :return: the list of matching apps, each trimmed to ID, name, title, mode
    URL and dashboard URL.
    """

    def map_app(app, config):
        """
        Creates the abbreviated data dictionary for the specified app and config
        information.

        :param app: the raw app data to start with.
        :param config: the configuration data to use.
        :return: the abbreviated app data dictionary.
        """
        return {
            "id": app["id"],
            "name": app["name"],
            "title": app["title"],
            "app_mode": AppModes.get_by_ordinal(app["app_mode"]).name(),
            "url": app["url"],
            "config_url": config["config_url"],
        }

    def mapping_filter(client, app):
        """
        Mapping/filter function for retrieving apps.  We only keep apps
        that have an app mode of static or Jupyter notebook.  The data
        for the apps we keep is an abbreviated subset.

        :param client: the client object to use for RStudio Connect calls.
        :param app: the current app from Connect.
        :return: the abbreviated data for the app or None.
        """
        # Only keep apps that match our app modes.
        app_mode = AppModes.get_by_ordinal(app["app_mode"])
        if app_mode not in (AppModes.STATIC, AppModes.JUPYTER_NOTEBOOK):
            return None

        config = client.app_config(app["id"])
        connect_server.handle_bad_response(config)

        return map_app(app, config)

    apps = retrieve_matching_apps(
        connect_server,
        filters={"filter": "min_role:editor", "search": app_title},
        mapping_function=mapping_filter,
        limit=5,
    )

    if app_id:
        found = next((app for app in apps if app["id"] == app_id), None)

        if not found:
            try:
                app = get_app_info(connect_server, app_id)
                mode = AppModes.get_by_ordinal(app["app_mode"])
                if mode in (AppModes.STATIC, AppModes.JUPYTER_NOTEBOOK):
                    apps.append(map_app(app, get_app_config(connect_server, app_id)))
            except RSConnectException:
                logger.debug('Error getting info for previous app_id "%s", skipping.', app_id)

    return apps


def find_unique_name(connect_server, name):
    """
    Poll through existing apps to see if anything with a similar name exists.
    If so, start appending numbers until a unique name is found.

    :param connect_server: the Connect server information.
    :param name: the default name for an app.
    :return: the name, potentially with a suffixed number to guarantee uniqueness.
    """
    existing_names = retrieve_matching_apps(
        connect_server,
        filters={"search": name},
        mapping_function=lambda client, app: app["name"],
    )

    if name in existing_names:
        suffix = 1
        test = "%s%d" % (name, suffix)
        while test in existing_names:
            suffix = suffix + 1
            test = "%s%d" % (name, suffix)
        name = test

    return name
