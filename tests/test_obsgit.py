import contextlib
import importlib
import os
import pathlib
import tempfile
import unittest
import unittest.mock
import xml.etree.ElementTree as ET

# Import the "obsgit" CLI as a module
file_name = pathlib.Path(pathlib.Path(__file__).parent, "..", "obsgit").resolve()
module_name = file_name.name
spec = importlib.util.spec_from_loader(
    module_name, importlib.machinery.SourceFileLoader(module_name, str(file_name))
)
obsgit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(obsgit)


class TestReadConfig(unittest.TestCase):
    config_filename = pathlib.Path("/tmp/config")

    def setUp(self):
        self._remove_config_filename()

    def tearDown(self):
        self._remove_config_filename()

    def _remove_config_filename(self):
        try:
            self.config_filename.unlink()
        except FileNotFoundError:
            pass

    def test_default_content(self):
        with open(os.devnull, "w") as devnull:
            with contextlib.redirect_stdout(devnull):
                config = obsgit.read_config(self.config_filename)
        self.assertEqual(config["import"]["url"], "https://api.opensuse.org")
        self.assertEqual(config["import"]["username"], os.getlogin())
        self.assertEqual(config["import"]["password"], "password")
        self.assertEqual(config["export"]["url"], "https://api.opensuse.org")
        self.assertEqual(config["export"]["username"], os.getlogin())
        self.assertEqual(config["export"]["password"], "password")
        self.assertEqual(
            config["export"]["storage"], f"home:{os.getlogin()}:storage/files"
        )

    def test_default_content_when_url(self):
        with open(os.devnull, "w") as devnull:
            with contextlib.redirect_stdout(devnull):
                config = obsgit.read_config(
                    self.config_filename, url="https://api.suse.de"
                )
        self.assertEqual(config["import"]["url"], "https://api.suse.de")
        self.assertEqual(config["import"]["username"], os.getlogin())
        self.assertEqual(config["import"]["password"], "password")
        self.assertEqual(config["export"]["url"], "https://api.suse.de")
        self.assertEqual(config["export"]["username"], os.getlogin())
        self.assertEqual(config["export"]["password"], "password")
        self.assertEqual(
            config["export"]["storage"], f"home:{os.getlogin()}:storage/files"
        )

    def test_default_content_when_username(self):
        with open(os.devnull, "w") as devnull:
            with contextlib.redirect_stdout(devnull):
                config = obsgit.read_config(self.config_filename, username="user")
        self.assertEqual(config["import"]["url"], "https://api.opensuse.org")
        self.assertEqual(config["import"]["username"], "user")
        self.assertEqual(config["import"]["password"], "password")
        self.assertEqual(config["export"]["url"], "https://api.opensuse.org")
        self.assertEqual(config["export"]["username"], "user")
        self.assertEqual(config["export"]["password"], "password")
        self.assertEqual(config["export"]["storage"], f"home:user:storage/files")

    def test_default_content_when_password(self):
        with open(os.devnull, "w") as devnull:
            with contextlib.redirect_stdout(devnull):
                config = obsgit.read_config(self.config_filename, password="secret")
        self.assertEqual(config["import"]["url"], "https://api.opensuse.org")
        self.assertEqual(config["import"]["username"], os.getlogin())
        self.assertEqual(config["import"]["password"], "secret")
        self.assertEqual(config["export"]["url"], "https://api.opensuse.org")
        self.assertEqual(config["export"]["username"], os.getlogin())
        self.assertEqual(config["export"]["password"], "secret")
        self.assertEqual(
            config["export"]["storage"], f"home:{os.getlogin()}:storage/files"
        )

    def test_default_persmissions(self):
        with open(os.devnull, "w") as devnull:
            with contextlib.redirect_stdout(devnull):
                obsgit.read_config(self.config_filename)
        self.assertTrue(self.config_filename.exists())
        self.assertEqual(self.config_filename.stat().st_mode, 33152)

    def test_custom_content(self):
        with open(self.config_filename, "w") as f:
            f.write(
                """
[import]
url = https://api.import.com
username = user_import
password = passwd_import

[export]
url = https://api.export.com
username = user_export
password = passwd_export
storage = project:storage/files
"""
            )
        config = obsgit.read_config(self.config_filename)
        self.assertEqual(config["import"]["url"], "https://api.import.com")
        self.assertEqual(config["import"]["username"], "user_import")
        self.assertEqual(config["import"]["password"], "passwd_import")
        self.assertEqual(config["export"]["url"], "https://api.export.com")
        self.assertEqual(config["export"]["username"], "user_export")
        self.assertEqual(config["export"]["password"], "passwd_export")
        self.assertEqual(config["export"]["storage"], f"project:storage/files")


class TestAsyncOBS(unittest.IsolatedAsyncioTestCase):
    @unittest.mock.patch.object(obsgit, "aiohttp")
    def test_open(self, aiohttp):
        obs = obsgit.AsyncOBS("https://api.example.local", "user", "secret")
        self.assertEqual(obs.url, "https://api.example.local")
        self.assertEqual(obs.username, "user")
        aiohttp.BasicAuth.assert_called_once_with("user", "secret")
        self.assertNotEqual(obs.client, None)

    async def test_close(self):
        obs = obsgit.AsyncOBS("https://api.example.local", "user", "secret")
        await obs.close()
        self.assertEqual(obs.client, None)

    async def test_create_enabled_project(self):
        obs = obsgit.AsyncOBS("https://api.example.local", "user", "secret")
        with unittest.mock.patch.object(obs, "authorized", return_value=True):
            with unittest.mock.patch.object(obs, "exists", return_value=False):
                with unittest.mock.patch.object(
                    obs, "client", new_callable=unittest.mock.AsyncMock
                ) as client:
                    await obs.create("myproject")
                    client.put.assert_called_once_with(
                        "https://api.example.local/source/myproject/_meta",
                        data=(
                            '<project name="myproject"><title/><description/>'
                            '<person userid="user" role="maintainer"/></project>'
                        ),
                    )
        await obs.close()

    async def test_create_disabled_project(self):
        obs = obsgit.AsyncOBS("https://api.example.local", "user", "secret")
        with unittest.mock.patch.object(obs, "authorized", return_value=True):
            with unittest.mock.patch.object(obs, "exists", return_value=False):
                with unittest.mock.patch.object(
                    obs, "client", new_callable=unittest.mock.AsyncMock
                ) as client:
                    await obs.create("myproject", disabled=True)
                    client.put.assert_called_once_with(
                        "https://api.example.local/source/myproject/_meta",
                        data=(
                            '<project name="myproject"><title/><description/>'
                            '<person userid="user" role="maintainer"/><build>'
                            "<disable/></build><publish><disable/></publish>"
                            "<useforbuild><disable/></useforbuild></project>"
                        ),
                    )
        await obs.close()

    async def test_create_non_authorized_project(self):
        obs = obsgit.AsyncOBS("https://api.example.local", "user", "secret")
        with unittest.mock.patch.object(obs, "authorized", return_value=False):
            with unittest.mock.patch.object(obs, "exists", return_value=False):
                with unittest.mock.patch.object(
                    obs, "client", new_callable=unittest.mock.AsyncMock
                ) as client:
                    await obs.create("myproject")
                    client.put.assert_not_called()
        await obs.close()

    async def test_create_existent_project(self):
        obs = obsgit.AsyncOBS("https://api.example.local", "user", "secret")
        with unittest.mock.patch.object(obs, "authorized", return_value=True):
            with unittest.mock.patch.object(obs, "exists", return_value=True):
                with unittest.mock.patch.object(
                    obs, "client", new_callable=unittest.mock.AsyncMock
                ) as client:
                    await obs.create("myproject")
                    client.put.assert_not_called()
        await obs.close()

    async def test_create_enabled_package(self):
        obs = obsgit.AsyncOBS("https://api.example.local", "user", "secret")
        with unittest.mock.patch.object(obs, "authorized", return_value=True):
            with unittest.mock.patch.object(obs, "exists", side_effect=[True, False]):
                with unittest.mock.patch.object(
                    obs, "client", new_callable=unittest.mock.AsyncMock
                ) as client:
                    await obs.create("myproject", "mypackage")
                    client.put.assert_called_once_with(
                        "https://api.example.local/source/myproject/mypackage/_meta",
                        data=(
                            '<package name="mypackage" project="myproject">'
                            "<title/><description/></package>"
                        ),
                    )
        await obs.close()

    async def test_create_disabled_package(self):
        obs = obsgit.AsyncOBS("https://api.example.local", "user", "secret")
        with unittest.mock.patch.object(obs, "authorized", return_value=True):
            with unittest.mock.patch.object(obs, "exists", side_effect=[True, False]):
                with unittest.mock.patch.object(
                    obs, "client", new_callable=unittest.mock.AsyncMock
                ) as client:
                    await obs.create("myproject", "mypackage", disabled=True)
                    client.put.assert_called_once_with(
                        "https://api.example.local/source/myproject/mypackage/_meta",
                        data=(
                            '<package name="mypackage" project="myproject"><title/>'
                            "<description/><build><disable/></build><publish><disable/>"
                            "</publish><useforbuild><disable/></useforbuild></package>"
                        ),
                    )
        await obs.close()

    async def test_download(self):
        obs = obsgit.AsyncOBS("https://api.example.local", "user", "secret")
        with unittest.mock.patch.object(obs, "_download") as download:
            await obs.download(
                "myproject",
                "mypackage",
                "myfile",
                filename_path="filename",
                params=[("rev", "latest")],
            )
            download.assert_called_once_with(
                "source/myproject/mypackage/myfile",
                "filename",
                params=[("rev", "latest")],
            )
        await obs.close()

    async def test_upload(self):
        obs = obsgit.AsyncOBS("https://api.example.local", "user", "secret")
        with unittest.mock.patch.object(obs, "_upload") as upload:
            await obs.upload(
                "myproject", "mypackage", "myfile", filename_path="filename",
            )
            upload.assert_called_once_with(
                "source/myproject/mypackage/myfile",
                filename_path="filename",
                data=None,
                params=None,
            )
        await obs.close()

    async def test_delete(self):
        obs = obsgit.AsyncOBS("https://api.example.local", "user", "secret")
        with unittest.mock.patch.object(obs, "_delete") as delete:
            await obs.delete("myproject", "mypackage", "myfile")
            delete.assert_called_once_with(
                "source/myproject/mypackage/myfile", params=None,
            )
        await obs.close()

    async def test_transfer(self):
        obs = obsgit.AsyncOBS("https://api.example.local", "user", "secret")
        with unittest.mock.patch.object(obs, "_transfer") as transfer:
            await obs.transfer("myproject", "mypackage", "myfile", "to_myproject")
            transfer.assert_called_once_with(
                "source/myproject/mypackage/myfile",
                "source/to_myproject/mypackage/myfile",
                None,
                None,
            )
        await obs.close()

    async def test_packages(self):
        obs = obsgit.AsyncOBS("https://api.example.local", "user", "secret")
        with unittest.mock.patch.object(obs, "_xml") as xml:
            xml.return_value = ET.fromstring(
                '<directory count="2"><entry name="package1"/>'
                '<entry name="package2"/></directory>'
            )
            packages = await obs.packages("myproject")
            self.assertEqual(packages, ["package1", "package2"])
        await obs.close()

    async def test_files_md5_revision(self):
        obs = obsgit.AsyncOBS("https://api.example.local", "user", "secret")
        with unittest.mock.patch.object(obs, "_xml") as xml:
            xml.return_value = ET.fromstring(
                '<directory name="mypackage" rev="5" vrev="5" srcmd5="srcmd5">'
                '<entry name="file1" md5="md51" size="1024" mtime="1234567890"/>'
                '<entry name="file2" md5="md52" size="1024" mtime="1234567890"/>'
                "</directory>"
            )
            files_md5, revision = await obs.files_md5_revision("myproject", "mypackage")
            self.assertEqual(files_md5, [("file1", "md51"), ("file2", "md52")])
            self.assertEqual(revision, "5")
        await obs.close()

    async def test_files_md5_revision_linkinfo(self):
        obs = obsgit.AsyncOBS("https://api.example.local", "user", "secret")
        with unittest.mock.patch.object(obs, "_xml") as xml:
            xml.side_effect = [
                ET.fromstring(
                    '<directory name="mypackage" rev="4" vrev="4" srcmd5="srcmd51">'
                    '<linkinfo project="myproject" package="mypackage" srcmd5="srcmd51"'
                    ' baserev="baserev1" xsrcmd5="xsrcmd51" lsrcmd5="lsrcmd51"/>'
                    '<entry name="_link" md5="md50" size="1024" mtime="1234567890"/>'
                    '<entry name="file1" md5="md51" size="1024" mtime="1234567890"/>'
                    "</directory>"
                ),
                ET.fromstring(
                    '<directory name="mypackage" rev="5" vrev="5" srcmd5="srcmd52">'
                    '<linkinfo project="myproject" package="mypackage" srcmd5="srcmd52"'
                    ' baserev="baserev2" xsrcmd5="xsrcmd52" lsrcmd5="lsrcmd52"/>'
                    '<entry name="file1" md5="md51" size="1024" mtime="1234567890"/>'
                    '<entry name="file2" md5="md52" size="1024" mtime="1234567890"/>'
                    "</directory>"
                ),
            ]
            files_md5, revision = await obs.files_md5_revision("myproject", "mypackage")
            self.assertEqual(files_md5, [("file1", "md51"), ("file2", "md52")])
            self.assertEqual(revision, "xsrcmd51")
        await obs.close()


class TestGit(unittest.IsolatedAsyncioTestCase):
    def test_exists_and_create(self):
        with tempfile.TemporaryDirectory() as tmp:
            git = obsgit.Git(tmp)
            self.assertFalse(git.exists())
            git.create()
            self.assertTrue(git.exists())

    async def test_delete_package(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = pathlib.Path(tmp)
            git = obsgit.Git(tmp)

            package_path = tmp / "mypackage"
            package_path.mkdir()

            self.assertTrue(package_path.exists())
            await git.delete("mypackage")
            self.assertFalse(package_path.exists())

    async def test_delete_filename(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = pathlib.Path(tmp)
            git = obsgit.Git(tmp)

            package_path = tmp / "mypackage"
            package_path.mkdir()

            filename_path = package_path / "myfile"
            filename_path.touch()

            self.assertTrue(package_path.exists())
            self.assertTrue(filename_path.exists())
            await git.delete("mypackage", "myfile")
            self.assertTrue(package_path.exists())
            self.assertFalse(filename_path.exists())

    async def test_packages(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = pathlib.Path(tmp)
            git = obsgit.Git(tmp)

            for package in ("mypackage1", "mypackage2", ".git", ".obs"):
                (tmp / package).mkdir()

            self.assertEqual(git.packages(), ["mypackage1", "mypackage2"])

    async def test_files_md5(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = pathlib.Path(tmp)
            git = obsgit.Git(tmp)

            package_path = tmp / "mypackage"
            package_path.mkdir()

            for filename in ("myfile1", "myfile2"):
                with (package_path / filename).open("w") as f:
                    f.write(filename)

            self.assertEqual(
                list(await git.files_md5("mypackage")),
                [
                    ("myfile1", "52a082e3940c1bda8306223103eaab28"),
                    ("myfile2", "549d8b648caf7cce417751c0fbe15c7a"),
                ],
            )


class TestStorage(unittest.IsolatedAsyncioTestCase):
    async def test_storage(self):
        obs = obsgit.AsyncOBS("https://api.example.local", "user", "secret")
        with unittest.mock.patch.object(
            obs, "files_md5_revision"
        ) as obs_files_md5_revision:
            obs_files_md5_revision.return_value = (
                [("md51", "md51"), ("md52", "md52")],
                None,
            )
            storage = await obsgit.Storage(obs, "project/package")

        self.assertEqual(storage.project, "project")
        self.assertEqual(storage.package, "package")
        self.assertEqual(storage.index, {"md51", "md52"})
        await obs.close()

    async def test_transfer(self):
        obs = obsgit.AsyncOBS("https://api.example.local", "user", "secret")
        with unittest.mock.patch.object(
            obs, "files_md5_revision"
        ) as obs_files_md5_revision:
            obs_files_md5_revision.return_value = (
                [("md51", "md51"), ("md52", "md52")],
                None,
            )
            storage = await obsgit.Storage(obs, "project/package")

        with unittest.mock.patch.object(obs, "transfer") as obs_transfer:
            await storage.transfer("md51", "myproject", "mypackage", "myfile", obs)
            obs_transfer.assert_called_once_with(
                "project", "package", "md51", "myproject", "mypackage", "myfile", obs
            )

        await obs.close()


class TestExporterIsBinary(unittest.TestCase):
    unknown_filename = pathlib.Path("/tmp/unknown")

    def setUp(self):
        self._remove_unknown_filename()

    def tearDown(self):
        self._remove_unknown_filename()

    def _remove_unknown_filename(self):
        try:
            self.unknown_filename.unlink()
        except FileNotFoundError:
            pass

    def test_is_binary_shortcut(self):
        self.assertTrue(obsgit.Exporter.is_binary("foo.tar.gz"))

    def test_is_non_binary_shorcut(self):
        self.assertFalse(obsgit.Exporter.is_binary("foo.spec"))

    def test_is_non_binary_exception_shorcut(self):
        self.assertTrue(obsgit.Exporter.is_binary("foo.obscpio"))

    def test_is_binary(self):
        with open(self.unknown_filename, "wb") as f:
            f.write(b"MZ\xea\x07\x00\xc0\x07\x8c")
        self.assertTrue(obsgit.Exporter.is_binary(self.unknown_filename))

    def test_is_non_binary(self):
        with open(self.unknown_filename, "w") as f:
            f.write("some text")
        self.assertFalse(obsgit.Exporter.is_binary(self.unknown_filename))


class TestExporter(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.obs = obsgit.AsyncOBS("https://api.example.local", "user", "secret")
        with unittest.mock.patch.object(
            self.obs, "files_md5_revision", return_value=[(), None]
        ):
            self.storage = await obsgit.Storage(self.obs, "project/package")
        self.git = obsgit.Git("/tmp/git")
        self.exporter = obsgit.Exporter(self.obs, self.git, self.storage)

    async def asyncTearDown(self):
        await self.obs.close()

    async def test_project(self):
        packages_obs = ["package1", "package2"]
        packages_git = ["package2", "package3"]
        self.obs.packages = unittest.mock.AsyncMock(return_value=packages_obs)
        self.git.packages = unittest.mock.MagicMock(return_value=packages_git)

        self.exporter.project_metadata = unittest.mock.AsyncMock()
        self.exporter.package = unittest.mock.AsyncMock()
        self.git.delete = unittest.mock.AsyncMock()

        await self.exporter.project("myproject")

        self.exporter.project_metadata.assert_called_once_with("myproject")
        self.exporter.package.assert_has_calls(
            [
                unittest.mock.call("myproject", "package1"),
                unittest.mock.call("myproject", "package2"),
            ],
            any_order=True,
        )
        self.git.delete.assert_called_once_with("package3")

    async def test_project_metadata(self):
        self.obs.download = unittest.mock.AsyncMock()

        with tempfile.TemporaryDirectory() as tmp:
            tmp = pathlib.Path(tmp)
            self.git.path = tmp

            await self.exporter.project_metadata("myproject")

            self.assertTrue((tmp / ".obs").exists())

        self.obs.download.assert_has_calls(
            [
                unittest.mock.call(
                    "myproject", "_meta", filename_path=(tmp / ".obs" / "_meta")
                ),
                unittest.mock.call(
                    "myproject", "_project", filename_path=(tmp / ".obs" / "_project")
                ),
                unittest.mock.call(
                    "myproject",
                    "_attribute",
                    filename_path=(tmp / ".obs" / "_attribute")
                ),
                unittest.mock.call(
                    "myproject", "_config", filename_path=(tmp / ".obs" / "_config")
                ),
                unittest.mock.call(
                    "myproject", "_pattern", filename_path=(tmp / ".obs" / "_pattern")
                ),
            ],
            any_order=True,
        )

    async def test_package(self):
        files_md5_obs = (
            [("file1", "md51"), ("file2", "md52"), ("file3", "md531")],
            "revision",
        )
        files_md5_git = [("file2", "md52"), ("file3", "md532"), ("file4", "md54")]
        store_index = {"md52"}
        is_binary = {"file1", "file2"}

        self.obs.files_md5_revision = unittest.mock.AsyncMock(
            return_value=files_md5_obs
        )
        self.git.files_md5 = unittest.mock.AsyncMock(return_value=files_md5_git)
        self.storage.index = store_index

        self.exporter.package_metadata = unittest.mock.AsyncMock()
        self.obs.download = unittest.mock.AsyncMock()
        self.obs.upload = unittest.mock.AsyncMock()
        self.git.delete = unittest.mock.AsyncMock()

        with unittest.mock.patch.object(
            obsgit.Exporter,
            "is_binary",
            side_effect=lambda x: x.parts[-1] in is_binary,
        ) as exporter_is_binary:
            with tempfile.TemporaryDirectory() as tmp:
                tmp = pathlib.Path(tmp)
                self.git.path = tmp
                (tmp / "mypackage" / ".obs").mkdir(parents=True)

                await self.exporter.package("myproject", "mypackage")

                self.assertTrue((self.git.path / "mypackage").exists())
                self.exporter.package_metadata.assert_called_once_with(
                    "myproject", "mypackage"
                )
                self.obs.download.assert_has_calls(
                    [
                        unittest.mock.call(
                            "myproject",
                            "mypackage",
                            "file1",
                            filename_path=tmp / "mypackage" / "file1",
                            params=[("rev", "revision")],
                        ),
                        unittest.mock.call(
                            "myproject",
                            "mypackage",
                            "file3",
                            filename_path=tmp / "mypackage" / "file3",
                            params=[("rev", "revision")],
                        ),
                    ],
                    any_order=True,
                )
                self.git.delete.assert_has_calls(
                    [
                        unittest.mock.call("mypackage", "file4"),
                        # Remove because is a binary file
                        unittest.mock.call("mypackage", "file1"),
                    ]
                )
                exporter_is_binary.assert_has_calls(
                    [
                        unittest.mock.call(tmp / "mypackage" / "file1"),
                        unittest.mock.call(tmp / "mypackage" / "file3"),
                    ],
                    any_order=True,
                )
                self.obs.upload.assert_called_once_with(
                    "project",
                    "package",
                    "md51",
                    filename_path=(tmp / "mypackage" / "file1"),
                )
                with (tmp / "mypackage" / ".obs" / "files").open() as files:
                    self.assertEqual(files.read(), "file1\t\tmd51\nfile2\t\tmd52\n")

    async def test_package_metadata(self):
        self.obs.download = unittest.mock.AsyncMock()

        with tempfile.TemporaryDirectory() as tmp:
            tmp = pathlib.Path(tmp)
            self.git.path = tmp
            (tmp / "mypackage").mkdir()

            await self.exporter.package_metadata("myproject", "mypackage")

            self.assertTrue((tmp / "mypackage" / ".obs").exists())

        self.obs.download.assert_has_calls(
            [
                unittest.mock.call(
                    "myproject",
                    "mypackage",
                    "_meta",
                    filename_path=(tmp / "mypackage" / ".obs" / "_meta"),
                ),
                unittest.mock.call(
                    "myproject",
                    "mypackage",
                    "_attribute",
                    filename_path=(tmp / "mypackage" / ".obs" / "_attribute"),
                ),
                unittest.mock.call(
                    "myproject",
                    "mypackage",
                    "_history",
                    filename_path=(tmp / "mypackage" / ".obs" / "_history"),
                ),
            ],
            any_order=True,
        )


if __name__ == "__main__":
    unittest.main()
