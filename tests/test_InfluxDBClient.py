import http.server
import json
import logging
import os
import threading
import unittest
from io import StringIO

import httpretty
import pytest
from urllib3.exceptions import NewConnectionError, HTTPError

from influxdb_client import InfluxDBClient, Point
from influxdb_client.client.write_api import WriteOptions, WriteType, SYNCHRONOUS
from tests.base_test import BaseTest


class InfluxDBClientTest(unittest.TestCase):

    def tearDown(self) -> None:
        if self.client:
            self.client.close()
        if hasattr(self, 'httpd'):
            self.httpd.shutdown()
        if hasattr(self, 'httpd_thread'):
            self.httpd_thread.join()

    def test_default_conf(self):
        self.client = InfluxDBClient(url="http://localhost:8086", token="my-token", org="my-org")
        self.assertIsNotNone(self.client.api_client.configuration.connection_pool_maxsize)

    def test_TrailingSlashInUrl(self):
        self.client = InfluxDBClient(url="http://localhost:8086", token="my-token", org="my-org")
        self.assertEqual('http://localhost:8086', self.client.api_client.configuration.host)

        self.client = InfluxDBClient(url="http://localhost:8086/", token="my-token", org="my-org")
        self.assertEqual('http://localhost:8086', self.client.api_client.configuration.host)

    def test_ConnectToSelfSignedServer(self):
        self._start_http_server()

        self.client = InfluxDBClient(f"https://localhost:{self.httpd.server_address[1]}",
                                     token="my-token", verify_ssl=False)
        ping = self.client.ping()

        self.assertTrue(ping)

    def test_certificate_file(self):
        self._start_http_server()

        self.client = InfluxDBClient(f"https://localhost:{self.httpd.server_address[1]}",
                                     token="my-token", verify_ssl=True,
                                     ssl_ca_cert=f'{os.path.dirname(__file__)}/server.pem')
        ping = self.client.ping()

        self.assertTrue(ping)

    def test_init_from_ini_file(self):
        self.client = InfluxDBClient.from_config_file(f'{os.path.dirname(__file__)}/config.ini')

        self.assertConfig()

    def test_init_from_toml_file(self):
        self.client = InfluxDBClient.from_config_file(f'{os.path.dirname(__file__)}/config.toml')

        self.assertConfig()

    def test_init_from_json_file(self):
        self.client = InfluxDBClient.from_config_file(f'{os.path.dirname(__file__)}/config.json')

        self.assertConfig()

    def assertConfig(self):
        self.assertEqual("http://localhost:8086", self.client.url)
        self.assertEqual("my-org", self.client.org)
        self.assertEqual("my-token", self.client.token)
        self.assertEqual(6000, self.client.api_client.configuration.timeout)
        self.assertEqual(3, len(self.client.default_tags))
        self.assertEqual("132-987-655", self.client.default_tags["id"])
        self.assertEqual("California Miner", self.client.default_tags["customer"])
        self.assertEqual("${env.data_center}", self.client.default_tags["data_center"])
        self.assertEqual(55, self.client.api_client.configuration.connection_pool_maxsize)
        self.assertEqual(False, self.client.api_client.configuration.auth_basic)
        self.assertEqual(["query", "operator"], self.client.profilers)

    def test_init_from_file_proxy(self):
        self.client = InfluxDBClient.from_config_file(f'{os.path.dirname(__file__)}/config-enabled-proxy.ini')
        self.assertConfig()
        self.assertEqual("http://proxy.domain.org:8080", self.client.api_client.configuration.proxy)

    def test_init_from_file_ssl_default(self):
        self.client = InfluxDBClient.from_config_file(f'{os.path.dirname(__file__)}/config.ini')

        self.assertTrue(self.client.api_client.configuration.verify_ssl)

    def test_init_from_file_ssl(self):
        self.client = InfluxDBClient.from_config_file(f'{os.path.dirname(__file__)}/config-disabled-ssl.ini')

        self.assertFalse(self.client.api_client.configuration.verify_ssl)

    def test_init_from_env_ssl_default(self):
        if os.getenv("INFLUXDB_V2_VERIFY_SSL"):
            del os.environ["INFLUXDB_V2_VERIFY_SSL"]
        self.client = InfluxDBClient.from_env_properties()

        self.assertTrue(self.client.api_client.configuration.verify_ssl)

    def test_init_from_env_ssl(self):
        os.environ["INFLUXDB_V2_SSL_CA_CERT"] = "/my/custom/path"
        self.client = InfluxDBClient.from_env_properties()

        self.assertEqual("/my/custom/path", self.client.api_client.configuration.ssl_ca_cert)

    def test_init_from_file_ssl_ca_cert_default(self):
        self.client = InfluxDBClient.from_config_file(f'{os.path.dirname(__file__)}/config.ini')

        self.assertIsNone(self.client.api_client.configuration.ssl_ca_cert)

    def test_init_from_file_ssl_ca_cert(self):
        self.client = InfluxDBClient.from_config_file(f'{os.path.dirname(__file__)}/config-ssl-ca-cert.ini')

        self.assertEqual("/path/to/my/cert", self.client.api_client.configuration.ssl_ca_cert)

    def test_init_from_env_ssl_ca_cert_default(self):
        if os.getenv("INFLUXDB_V2_SSL_CA_CERT"):
            del os.environ["INFLUXDB_V2_SSL_CA_CERT"]
        self.client = InfluxDBClient.from_env_properties()

        self.assertIsNone(self.client.api_client.configuration.ssl_ca_cert)

    def test_init_from_env_ssl_ca_cert(self):
        os.environ["INFLUXDB_V2_SSL_CA_CERT"] = "/my/custom/path/to/cert"
        self.client = InfluxDBClient.from_env_properties()

        self.assertEqual("/my/custom/path/to/cert", self.client.api_client.configuration.ssl_ca_cert)

    def test_init_from_env_connection_pool_maxsize(self):
        os.environ["INFLUXDB_V2_CONNECTION_POOL_MAXSIZE"] = "29"
        self.client = InfluxDBClient.from_env_properties()

        self.assertEqual(29, self.client.api_client.configuration.connection_pool_maxsize)

    def _start_http_server(self):
        import http.server
        import ssl
        # Disable unverified HTTPS requests
        import urllib3
        urllib3.disable_warnings()
        # Configure HTTP server
        self.httpd = http.server.HTTPServer(('localhost', 0), ServerWithSelfSingedSSL)
        self.httpd.socket = ssl.wrap_socket(self.httpd.socket, certfile=f'{os.path.dirname(__file__)}/server.pem',
                                            server_side=True)
        # Start server at background
        self.httpd_thread = threading.Thread(target=self.httpd.serve_forever)
        self.httpd_thread.start()

    def test_write_context_manager(self):

        with InfluxDBClient.from_env_properties(self.debug) as self.client:
            api_client = self.client.api_client
            with self.client.write_api(write_options=WriteOptions(write_type=WriteType.batching)) as write_api:
                write_api_test = write_api
                write_api.write(bucket="my-bucket",
                                record=Point("h2o_feet")
                                .tag("location", "coyote_creek")
                                .field("level water_level", 5.0))
                self.assertIsNotNone(write_api._subject)
                self.assertIsNotNone(write_api._disposable)

            self.assertIsNone(write_api_test._subject)
            self.assertIsNone(write_api_test._disposable)
            self.assertIsNotNone(self.client.api_client)
            self.assertIsNotNone(self.client.api_client.rest_client.pool_manager)

        self.assertIsNone(api_client._pool)
        self.assertIsNone(self.client.api_client)

    def test_timeout_as_float(self):
        self.client = InfluxDBClient(url="http://localhost:8088", token="my-token", org="my-org", timeout=1000.5)
        self.assertEqual(1000.5, self.client.api_client.configuration.timeout)
        with pytest.raises(HTTPError) as e:
            write_api = self.client.write_api(write_options=SYNCHRONOUS)
            write_api.write(bucket="my-bucket", org="my-org", record="mem,tag=a value=1")
        self.assertIn("Failed to establish a new connection", str(e.value))


class InfluxDBClientTestIT(BaseTest):
    httpRequest = []

    def tearDown(self) -> None:
        super(InfluxDBClientTestIT, self).tearDown()
        if hasattr(self, 'httpd'):
            self.httpd.shutdown()
        if hasattr(self, 'httpd_thread'):
            self.httpd_thread.join()
        InfluxDBClientTestIT.httpRequest = []

    def test_proxy(self):
        self._start_proxy_server()

        self.client.close()
        self.client = InfluxDBClient(url=self.host,
                                     token=self.auth_token,
                                     proxy=f"http://localhost:{self.httpd.server_address[1]}",
                                     proxy_headers={'ProxyHeader': 'Val'})
        ready = self.client.ready()
        self.assertEqual(ready.status, "ready")
        self.assertEqual(1, len(InfluxDBClientTestIT.httpRequest))
        self.assertEqual('Val', InfluxDBClientTestIT.httpRequest[0].headers.get('ProxyHeader'))

    def test_ping(self):
        ping = self.client.ping()
        self.assertTrue(ping)

    def test_ping_not_running_instance(self):
        client_not_running = InfluxDBClient("http://localhost:8099", token="my-token", debug=True)
        ping = client_not_running.ping()
        self.assertFalse(ping)
        client_not_running.close()

    def test_version(self):
        version = self.client.version()
        self.assertTrue(len(version) > 0)

    def test_version_not_running_instance(self):
        client_not_running = InfluxDBClient("http://localhost:8099", token="my-token", debug=True)
        with self.assertRaises(NewConnectionError):
            client_not_running.version()

        client_not_running.close()

    def test_username_password_authorization(self):
        self.client.close()
        self.client = InfluxDBClient(url=self.host, username="my-user", password="my-password", debug=True)
        self.client.query_api().query("buckets()", "my-org")

    def _start_proxy_server(self):
        import http.server
        import urllib.request

        class ProxyHTTPRequestHandler(http.server.SimpleHTTPRequestHandler):

            def do_GET(self):
                InfluxDBClientTestIT.httpRequest.append(self)
                self.send_response(200)
                self.send_header('Content-type', 'application/json')
                self.end_headers()
                self.copyfile(urllib.request.urlopen(self.path), self.wfile)

        self.httpd = http.server.HTTPServer(('localhost', 0), ProxyHTTPRequestHandler)
        self.httpd_thread = threading.Thread(target=self.httpd.serve_forever)
        self.httpd_thread.start()


class InfluxDBClientTestMock(unittest.TestCase):

    def setUp(self) -> None:
        httpretty.enable()
        httpretty.reset()

    def tearDown(self) -> None:
        if self.influxdb_client:
            self.influxdb_client.close()
        httpretty.disable()

    def test_init_without_token(self):
        httpretty.register_uri(httpretty.POST, uri="http://localhost/api/v2/query", status=200, body="")
        self.influxdb_client = InfluxDBClient("http://localhost")
        self.assertIsNotNone(self.influxdb_client)
        self.influxdb_client.query_api().query("buckets()", "my-org")

    def test_redacted_auth_header(self):
        httpretty.register_uri(httpretty.POST, uri="http://localhost/api/v2/query", status=200, body="")
        self.influxdb_client = InfluxDBClient("http://localhost", "my-token", debug=True)

        log_stream = StringIO()
        logger = logging.getLogger("influxdb_client.client.http")
        logger.addHandler(logging.StreamHandler(log_stream))

        self.influxdb_client.query_api().query("buckets()", "my-org")
        requests = httpretty.httpretty.latest_requests
        self.assertEqual(1, len(requests))
        self.assertEqual("Token my-token", requests[0].headers["Authorization"])

        self.assertIn("Authorization: ***", log_stream.getvalue())


class ServerWithSelfSingedSSL(http.server.SimpleHTTPRequestHandler):
    def _set_headers(self):
        self.send_response(200)
        self.send_header('Content-type', 'application/json')
        self.end_headers()

    def do_GET(self):
        self._set_headers()
        response = json.dumps(
            dict(name="influxdb", message="ready for queries and writes", status="pass", checks=[], version="2.0.0",
                 commit="abcdefgh")).encode('utf-8')
        self.wfile.write(response)
