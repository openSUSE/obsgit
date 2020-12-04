import setuptools

with open("README.md", "r") as f:
    long_description = f.read()


setuptools.setup(
    name="obsgit",
    version="0.1.0",
    author="Alberto Planas",
    author_email="aplanas@gmail.com",
    description="Simple bridge between Open Build Server and Git",
    long_description=long_description,
    long_description_content_type="text/markdown",
    url="https://github.com/aplanas/obsgit",
    packages=setuptools.find_packages(),
    python_requires=">=3.6",
    classifiers=[
        "Development Status :: 5 - Production/Stable",
        "Environment :: Console",
        "Intended Audience :: Developers",
        "Intended Audience :: Information Technology",
        "Intended Audience :: System Administrators",
        "License :: OSI Approved :: Apache Software License",
        "Programming Language :: Python :: 3",
        "Topic :: Software Development :: Build Tools",
        "Topic :: System :: Archiving :: Packaging",
    ],
    entry_points={
        "console_scripts": ["obsgit=obsgit.obsgit:main"],
    },
    install_requires=[
        "aiohttp",
        "pygit2",
    ],
)
