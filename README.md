# obsgit

A simple bridge between Open Build Server and git.

These tools can be used to export a project stored in OBS into a local
git repository, and later imported from git to the same (or different)
OBS server.

## Installation
Install `obsgit` using Python Pip:

```bash
pip install git+https://github.com/aplanas/obsgit.git
```

After installed, `obsgit` will be registered as a command-line tool.

## Configuration

`obsgit` requires a configuration file to adjust the parameters of the
different OBS services, the remote storage for big files, and some
configuration of the layout of the local git repository.

You can generate a default configuration file with:

```bash
obsgit create-config
```

This command accepts parameters to adjust the configuration, but you
can also edit the generated file to set the passwords and the
different URL for the APIs:

```ini
[export]
# API URL for the build system where we will export from (to git)
url = https://api.opensuse.org
# Login credentials
username = user
password = password
# Only if OBS is configured with SSH authentication (like in IBS)
ssh-key = id_rsa
# What to do when obsgit read a linked package:
# - always: always expand the _link, downloading the expanded source
# - never: never expand, download only the _link file. If link is
#     pointing to a different project, generate an error for this package
# - auto: expand the link only if point to a different project
link = never

[import]
# API URL for the build system where we will upload the project (from git)
url = https://api.opensuse.org
username = user
password = password

[git]
# Directory name used to store all the packages. If missing, the packages
# will be stored under the git repository
prefix = packages

[storage]
# Type of repository where to store large binary files
# - lfs: use git-lfs protocol (require git-lfs binary)
# - obs: use OBS / IBS to store the large files
type = lfs
# (obs) API URL for the build system to store files
# url = https://api.opensuse.org
# username = user
# password = password
# (obs) Repository and package where to store the files
# storage = home:user:storage/files
```

## Export from OBS to git

The `export` sub-command can be used to read all the metadata of an
OBS project, the list of packages and the content, and download them
in a local git repository. This information is organized with goals in
mind. One is to collect all the information required to re-publish the
project and packages in a different OBS instance, and the other one is
to delegate into git the management of the package assets (changelog,
spec files, patches, etc).

To export a full project:

```bash
obsgit export openSUSE:Factory ~/Project/factory-git
```

If required, this command will initialize the local git repository
given as a second parameter, and using the credentials from the
configuration file, download all the metadata and packages from the
project.

We can also export a single package:

```bash
obsgit export --package gcc openSUSE:Factory ~/Project/factory-git
```

Both commands will read the metadata that OBS stores for the packages
and or the project, and will replace the one that is stored in the
local git repository. Sometimes we do not want to replace the local
metadata, and for that, we can use the `--skip-all-project-meta` and
`--skip-all-package-meta` parameters, or `--skip-project-meta` if we
want only to skip the update for the `_meta` metadata. For example:

```bash
obsgit export --skip-project-meta openSUSE:Factory ~/Project/factory-git
```

If we are using the `lfs` extension of git, the export will create a
`.gitattributes` file that references all the detected binary
files. You can use the `git lfs` commands to add or remove tracked
files, add them to the index and do the commit.

When the storage is configured to use `obs`, the binary files are
uploaded into the storage server and tracked in the
`<package>/.obs/files` metadata file.

## Import from git to OBS

We can re-create the original project that we exported from OBS to git
into a different build service. To do that we can use the `import`
sub-command:

```bash
obsgit import ~/Project/factory-git home:user:import
```

In the same way, we can use the `--package` parameter to restrict the
import to a single package, and the different skip metadata
parameters.

During the `export` stage, the tool collected the metadata information
of the project and for each package. This metadata will contain
information about users that do not exist in the new imported OBS, and
also will contain references to the name of the exported project.

The `import` stage will try to re-allocate the project into the new
OBS location, editing on the fly the metadata. This edit is basically
a project name replacement: every time the old project name is found
gets replaced with the new project name. If you edit the project name
in the metadata, please, consider updating all the metadata
information for the rest of the files, as `obsgit` will not be able to
re-allocate the project anymore.

## Updating the release version of packages

We can export into an external file OBS revision of the packages
inside a project, and use this number to adjust the revision in the
spec file transparently.

To fetch only the revision number of the packages, without exporting
anything else:

```bash
obsgit export --only-export-revisions revisions.csv openSUSE:Factory ~/Project/factory-git
```

This will create a local file `revisions.csv` that will contain the
name of the package and the last revision (number of commits)
registered by OBS.

We can use this file to transparently replace the `Release: 0` present
in some spec files during the import.

```bash
obsgit import --adjust-release revisions.csv ~/Project/factory-git home:user:import
```

Optionally, you can provide a different CSV file generated, maybe,
analyzing a repository using a different tool.
