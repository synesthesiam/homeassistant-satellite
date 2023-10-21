#!/usr/bin/env python3
from pathlib import Path

import setuptools
from setuptools import setup

this_dir = Path(__file__).parent
module_dir = this_dir / "homeassistant_satellite"

# -----------------------------------------------------------------------------

# Load README in as long description
long_description: str = ""
readme_path = this_dir / "README.md"
if readme_path.is_file():
    long_description = readme_path.read_text(encoding="utf-8")

requirements = []
requirements_path = this_dir / "requirements.txt"
if requirements_path.is_file():
    with open(requirements_path, "r", encoding="utf-8") as requirements_file:
        requirements = requirements_file.read().splitlines()

version_path = module_dir / "VERSION"
with open(version_path, "r", encoding="utf-8") as version_file:
    version = version_file.read().strip()

# -----------------------------------------------------------------------------

setup(
    name="homeassistant_satellite",
    version=version,
    description="Voice satellite for Home Assistant",
    long_description=long_description,
    long_description_content_type="text/markdown",
    url="http://github.com/synesthesiam/homeassistant-satellite",
    author="Michael Hansen",
    author_email="mike@rhasspy.org",
    license="MIT",
    packages=setuptools.find_packages(),
    package_data={
        "homeassistant_satellite": ["VERSION", "py.typed", "models/silero_vad.onnx"],
    },
    install_requires=requirements,
    extras_require={
        "silerovad": ["onnxruntime>=1.10.0,<2", "numpy<1.26"],
        "webrtc": ["webrtc-noise-gain==1.2.3"],
        "pulseaudio": ["pasimple>=0.0.2", "pulsectl>=23.5.2"],
        "pyaudio": ["PyAudio==0.2.13"],
    },
    classifiers=[
        "Development Status :: 3 - Alpha",
        "Intended Audience :: Developers",
        "Topic :: Text Processing :: Linguistic",
        "License :: OSI Approved :: MIT License",
        "Programming Language :: Python :: 3.7",
        "Programming Language :: Python :: 3.8",
        "Programming Language :: Python :: 3.9",
        "Programming Language :: Python :: 3.10",
        "Programming Language :: Python :: 3.11",
    ],
    keywords="voice satellite home assistant",
    entry_points={
        'console_scripts': [
            'homeassistant-satellite = homeassistant_satellite:__main__.run'
        ]
    },
)
