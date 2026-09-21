from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import importlib.machinery
import importlib.util
import io
from http.client import IncompleteRead
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit
from urllib.request import HTTPHandler, build_opener
from urllib.response import addinfourl


SCRIPT = Path(__file__).resolve().parents[1] / "space"
loader = importlib.machinery.SourceFileLoader("space_cli", str(SCRIPT))
spec = importlib.util.spec_from_loader(loader.name, loader)
cli = importlib.util.module_from_spec(spec)
loader.exec_module(cli)

TOKEN = "fictional-cli-token"
URL = "http://127.0.0.1:5002"
STATUS = {"enabled": True, "commands": ["projects", "document", "todos", "inbox", "status"]}


class SpaceCLITests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config = self.root / "config" / "cli.json"
        environment = patch.dict(os.environ, {"SPACE_CLI_CONFIG": str(self.config)}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)

    def invoke(self, *arguments):
        output, errors = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            code = cli.main(list(arguments))
        self.assertNotIn(TOKEN, output.getvalue())
        self.assertNotIn(TOKEN, errors.getvalue())
        self.assertNotIn("Traceback", errors.getvalue())
        return code, output.getvalue(), errors.getvalue()

    def save(self, url=URL, token=TOKEN):
        cli.save_config(url, token)

    def test_configure_verifies_and_stores_owner_only_from_first_write(self):
        real_dump = json.dump
        observed_modes = []

        def observe_write(data, stream, **kwargs):
            observed_modes.append(stat.S_IMODE(os.fstat(stream.fileno()).st_mode))
            return real_dump(data, stream, **kwargs)

        with patch.object(cli.getpass, "getpass", return_value=TOKEN) as prompt, \
                patch.object(cli, "request_json", return_value=STATUS) as request, \
                patch.object(cli.json, "dump", side_effect=observe_write):
            code, output, errors = self.invoke("configure", "--url", URL + "/")
        self.assertEqual((code, errors), (0, ""))
        self.assertIn("verified", output)
        prompt.assert_called_once()
        request.assert_called_once_with(URL, TOKEN, "/api/cli/status")
        self.assertEqual(observed_modes, [0o600])
        self.assertEqual(stat.S_IMODE(self.config.stat().st_mode), 0o600)
        self.assertEqual(json.loads(self.config.read_text()), {"url": URL, "token": TOKEN})
        self.assertEqual(list(self.config.parent.iterdir()), [self.config])

    def test_configure_from_environment_and_invalid_token_does_not_replace_credentials(self):
        self.save()
        before = self.config.read_bytes()
        with patch.dict(os.environ, {"SPACE_CLI_TOKEN": "replacement", "SPACE_URL": URL}), \
                patch.object(cli.getpass, "getpass") as prompt, \
                patch.object(cli, "request_json", side_effect=cli.CLIError("CLI token rejected.")):
            code, output, errors = self.invoke("configure")
        self.assertEqual(code, 1)
        self.assertEqual(output, "")
        self.assertIn("rejected", errors)
        self.assertEqual(self.config.read_bytes(), before)
        prompt.assert_not_called()

    def test_hidden_prompt_must_not_fall_back_to_echoed_input(self):
        with patch.object(cli.getpass, "getpass", side_effect=cli.getpass.GetPassWarning("unavailable")):
            code, _, errors = self.invoke("configure", "--url", URL)
        self.assertEqual(code, 1)
        self.assertIn("hidden token prompt", errors)
        self.assertFalse(self.config.exists())

    def test_atomic_save_failure_keeps_previous_credentials_and_removes_tempfile(self):
        self.save()
        original = self.config.read_bytes()
        with patch.object(cli.os, "replace", side_effect=OSError("private-path")):
            with self.assertRaises(cli.CLIError) as error:
                cli.save_config(URL, "replacement")
        self.assertNotIn("private-path", str(error.exception))
        self.assertEqual(self.config.read_bytes(), original)
        self.assertEqual(list(self.config.parent.iterdir()), [self.config])

    def test_config_path_precedence(self):
        self.assertEqual(cli.config_path(), self.config)
        with patch.dict(os.environ, {"SPACE_CLI_CONFIG": "", "XDG_CONFIG_HOME": str(self.root / "xdg")}):
            self.assertEqual(cli.config_path(), self.root / "xdg" / "xo-space" / "cli.json")
        with patch.dict(os.environ, {"SPACE_CLI_CONFIG": "", "XDG_CONFIG_HOME": ""}), \
                patch.object(cli.Path, "home", return_value=self.root):
            self.assertEqual(cli.config_path(), self.root / ".config" / "xo-space" / "cli.json")

    def test_url_validation(self):
        for value in ("http://127.0.0.1:5002/", "http://localhost", "http://[::1]", "https://space.example.com"):
            with self.subTest(value=value):
                self.assertTrue(cli.normalize_url(value))
        for value in ("http://example.com", "http://192.168.1.4", "https://user:password@example.com",
                      "https://example.com/path", "https://example.com?", "https://example.com#",
                      "https://example.com:99999", "https://example.com:0", "file:///tmp/data",
                      "https://example.com\n", "https://example.com\\evil", "https://evil%2ecom"):
            with self.subTest(value=value), self.assertRaises(cli.CLIError):
                cli.normalize_url(value)
        self.assertEqual(cli.normalize_url("https://EXAMPLE.com:443/"), "https://example.com")

    def test_saved_token_is_never_sent_to_a_different_url(self):
        self.save()
        for arguments, environment in (
            (("projects", "--url", "https://other.example.com"), {}),
            (("projects",), {"SPACE_URL": "https://other.example.com"}),
            (("projects", "--url", "http://127.0.0.1:6000"), {}),
        ):
            with self.subTest(arguments=arguments), patch.dict(os.environ, environment), \
                    patch.object(cli, "request_json") as request:
                code, _, errors = self.invoke(*arguments)
            self.assertEqual(code, 1)
            self.assertIn("different Space URL", errors)
            request.assert_not_called()

    def test_explicit_environment_token_allows_url_override_and_args_win(self):
        self.save()
        with patch.dict(os.environ, {"SPACE_URL": "https://env.example.com", "SPACE_CLI_TOKEN": "override-token"}), \
                patch.object(cli, "request_json", return_value=STATUS) as request:
            code, _, errors = self.invoke("--url", "https://argument.example.com", "status")
        self.assertEqual((code, errors), (0, ""))
        request.assert_called_once_with("https://argument.example.com", "override-token", "/api/cli/status", {})

    def test_environment_credentials_work_without_reading_corrupt_local_config(self):
        self.config.parent.mkdir()
        self.config.write_text("broken")
        with patch.dict(os.environ, {"SPACE_URL": URL, "SPACE_CLI_TOKEN": TOKEN}), \
                patch.object(cli, "request_json", return_value=STATUS):
            code, _, errors = self.invoke("status", "--json")
        self.assertEqual((code, errors), (0, ""))

    def test_data_commands_send_expected_parameters_and_json(self):
        self.save()
        cases = [
            (("projects",), "/api/cli/projects", {"limit": 50, "offset": 0}, {"projects": [], "total": 0}),
            (("projects", "--limit", "4", "--offset", "2"), "/api/cli/projects", {"limit": 4, "offset": 2}, {"projects": []}),
            (("document", "my project", "PLAN.md"), "/api/cli/document", {"project_id": "my project", "document": "PLAN.md"}, {"content": "Plan"}),
            (("todos", "demo", "--limit", "3"), "/api/cli/todos", {"project_id": "demo", "limit": 3}, {"todos": []}),
            (("inbox",), "/api/cli/inbox", {"status": "open", "limit": 50}, {"items": []}),
            (("inbox", "--status", "done"), "/api/cli/inbox", {"status": "done", "limit": 50}, {"items": []}),
            (("status",), "/api/cli/status", {}, STATUS),
        ]
        for arguments, endpoint, parameters, response in cases:
            with self.subTest(arguments=arguments), patch.object(cli, "request_json", return_value=response) as request:
                code, output, errors = self.invoke(*arguments, "--json")
            self.assertEqual((code, errors), (0, ""))
            self.assertEqual(json.loads(output), response)
            request.assert_called_once_with(URL, TOKEN, endpoint, parameters)

    def test_invalid_arguments_never_make_requests(self):
        for arguments in (("projects", "--limit", "101"), ("projects", "--offset", "-1"),
                          ("inbox", "--status", "wrong"), ("document", "demo", ".env"),
                          ("status", "--token", TOKEN)):
            with self.subTest(arguments=arguments), patch.object(cli, "request_json") as request:
                code, _, errors = self.invoke(*arguments)
            self.assertEqual(code, 1)
            self.assertIn("Invalid command arguments", errors)
            request.assert_not_called()

    def test_terminal_controls_are_neutralized_and_json_preserves_data_safely(self):
        self.save()
        malicious = "Hello\x1b[2J\x1b]52;c;secret\x07\r\x9b31m\u202eWorld\nnext"
        with patch.object(cli, "request_json", return_value={"content": malicious}):
            code, output, _ = self.invoke("document", "demo", "README.md")
            json_code, json_output, _ = self.invoke("document", "demo", "README.md", "--json")
        self.assertEqual((code, json_code), (0, 0))
        for control in ("\x1b", "\x07", "\r", "\x9b", "\u202e"):
            self.assertNotIn(control, output)
            self.assertNotIn(control, json_output)
        self.assertEqual(json.loads(json_output)["content"], malicious)

    def test_http_request_is_get_encoded_authenticated_and_bounded(self):
        response = Mock()
        response.read.return_value = b'{"todos":[]}'
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        opener = Mock()
        opener.open.return_value = response
        with patch.object(cli, "build_opener", return_value=opener) as factory:
            result = cli.request_json(URL, TOKEN, "/api/cli/todos", {"project_id": "a b&c", "limit": 2})
        self.assertEqual(result, {"todos": []})
        request = opener.open.call_args.args[0]
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(request.get_header("Authorization"), "Bearer " + TOKEN)
        self.assertEqual(parse_qs(urlsplit(request.full_url).query), {"project_id": ["a b&c"], "limit": ["2"]})
        self.assertEqual(opener.open.call_args.kwargs, {"timeout": 15})
        response.read.assert_called_once_with(4 * 1024 * 1024 + 1)
        self.assertIsInstance(factory.call_args.args[0], cli.NoRedirect)

    def test_http_errors_are_safe_and_actionable(self):
        self.save()
        for status, hint in ((401, "token rejected"), (403, "Access was denied"),
                             (404, "disabled"), (503, "unavailable"), (302, "Redirects are refused")):
            failure = HTTPError("https://private.example.com?token=" + TOKEN, status, TOKEN, {}, io.BytesIO(TOKEN.encode()))
            opener = Mock()
            opener.open.side_effect = failure
            with self.subTest(status=status), patch.object(cli, "build_opener", return_value=opener):
                code, output, errors = self.invoke("status")
            self.assertEqual((code, output), (1, ""))
            self.assertIn(hint, errors)
            self.assertNotIn("private.example", errors)

    def test_redirect_handler_never_sends_a_second_request(self):
        received = []

        class RedirectingServer(HTTPHandler):
            def http_open(self, request):
                received.append(request)
                response = addinfourl(io.BytesIO(b""), {"Location": "https://other.example.com/stolen"}, request.full_url, 302)
                response.msg = "Found"
                return response

        opener = build_opener(cli.NoRedirect(), RedirectingServer())
        with patch.object(cli, "build_opener", return_value=opener):
            with self.assertRaises(cli.CLIError) as error:
                cli.request_json(URL, TOKEN, "/api/cli/status")
        self.assertIn("Redirects are refused", str(error.exception))
        self.assertEqual(len(received), 1)
        self.assertTrue(received[0].full_url.startswith(URL))

    def test_network_and_malformed_response_fail_without_tracebacks(self):
        self.save()
        opener = Mock()
        opener.open.side_effect = URLError("secret-url-with-" + TOKEN)
        with patch.object(cli, "build_opener", return_value=opener):
            code, _, errors = self.invoke("status")
        self.assertEqual(code, 1)
        self.assertIn("Could not reach Space", errors)
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.side_effect = IncompleteRead(b"partial")
        opener.open.side_effect = None
        opener.open.return_value = response
        with patch.object(cli, "build_opener", return_value=opener):
            code, _, errors = self.invoke("status")
        self.assertEqual(code, 1)
        self.assertIn("Could not reach Space", errors)
        for body in (b"not-json", b"[]", b"x" * (cli.MAX_RESPONSE_BYTES + 1)):
            response = io.BytesIO(body)
            opener = Mock()
            opener.open.return_value = response
            with self.subTest(length=len(body)), patch.object(cli, "build_opener", return_value=opener):
                code, _, errors = self.invoke("status")
            self.assertEqual(code, 1)
            self.assertTrue("invalid JSON" in errors or "4 MiB limit" in errors)

    def test_logout_removes_only_local_credentials(self):
        self.save()
        with patch.object(cli, "request_json") as request:
            code, output, errors = self.invoke("logout")
            second_code, _, _ = self.invoke("logout")
        self.assertEqual((code, second_code, errors), (0, 0, ""))
        self.assertFalse(self.config.exists())
        self.assertIn("server token remains valid", output)
        request.assert_not_called()


if __name__ == "__main__":
    unittest.main()
