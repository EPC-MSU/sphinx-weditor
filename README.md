# Sphinx-Weditor

Simple web editor for a Mercurial repository
with Sphinx documents.

## Install dependencies

    python3 -m venv venv
    ./venv/bin/pip install -e .
    ./venv/bin/pip install sphinx sphinxcontrib.bibtex

## Make config

    cp settings.conf.example settings.conf

And adjust them a little

## Run

    SETTINGS_FILE=settings.conf FLASK_APP=sphinx_weditor ./venv/bin/python flask run

