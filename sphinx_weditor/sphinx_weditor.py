import logging

import sys

import os
import subprocess

import bleach
from flask import Flask, render_template, redirect, send_file, request, flash, session
from flask_bootstrap import Bootstrap
from jinja2 import Markup
from bs4 import BeautifulSoup

app = Flask('sphinx_weditor')


def configure_app(config=None):
    logging.basicConfig(stream=sys.stdout,
                        format='[%(asctime)s] %(name)s[%(process)d] %(levelname)s -- %(message)s',
                        level=logging.DEBUG)
    logging.info("Configuring app")
    app.config.update(dict(
        DEBUG=True,
        SECRET_KEY=b'secret'
    ))
    app.config.update(config or {})
    app.config.from_envvar('SETTINGS_FILE', silent=True)

    Bootstrap(app)

    logging.info("Using doc root at {}".format(app.config.get('DOC_ROOT', None)))

    return app


@app.after_request
def apply_caching(response):
    response.headers["Cache-Control"] = 'no-cache, no-store, must-revalidate'
    return response


def include_raw(filename):
    from jinja2 import FileSystemLoader
    loader = FileSystemLoader(app.config['DOC_ROOT'])
    return Markup(loader.get_source(app.jinja_env, filename)[0])


app.jinja_env.globals['include_raw'] = include_raw


def find_rst_file(doc_path):
    with open(app.config['DOC_ROOT'] + '/' + doc_path, 'r', encoding='utf-8') as fp:
        soup = BeautifulSoup(fp, "html.parser")

    rst_rel = None
    elements = soup.find_all('div', 'footer')
    if elements:
        elements = elements[0].find_all('a', text='Page source')
        if elements:
            rst_rel = elements[0].attrs['href']

    if not rst_rel:
        return None

    logging.debug('Found rel to source ' + str(rst_rel))
    rst_rel = rst_rel.split('/')[-1]
    if rst_rel.endswith('.rst.txt'):
        rst_rel = rst_rel[0:-4]

    logging.debug('Bare name ' + str(rst_rel))

    for root, dirs, files in os.walk(app.config['DOC_ROOT'] + '/doc_src'):
        for name in files:
            if name == rst_rel:
                abs_path = root + '/' + name
                rel_path = os.path.relpath(abs_path, app.config['DOC_ROOT'])
                return rel_path

    return None


@app.route('/')
def handle_root():
    return redirect('/_viewer/index.html')


def checked_run(cmd: str, redirect_stdout: bool = True):
    kwargs = dict(shell=True,
                  check=False,
                  cwd=app.config['DOC_ROOT'])
    if redirect_stdout:
        kwargs['stdout'] = subprocess.PIPE
    else:
        kwargs['stderr'] = subprocess.PIPE

    logging.info("Running command: " + cmd)
    ret = subprocess.run(cmd, **kwargs)

    if redirect_stdout:
        redirected = ret.stdout.decode('utf-8')
    else:
        redirected = ret.stderr.decode('utf-8')

    if ret.returncode != 0:
        raise RuntimeError("Error at '{}': code {}, out {}".format(cmd,
                                                                   ret.returncode,
                                                                   redirected))
    return redirected


def process_save(content, commit_message, commit_author, rst_path, rst_file):
    if not commit_message:
        commit_message = 'Unnamed web commit'
    else:
        commit_message = bleach.clean(commit_message)

    if not commit_author:
        raise RuntimeError('Please say your name')
    else:
        commit_author = bleach.clean(commit_author)

    if not session.get('hg_author', None):
        session['hg_author'] = commit_author

    logging.debug("Writing to file {} {} bytes".format(rst_path, len(content)))

    content = "\n".join(content.splitlines())

    with open(rst_path, 'w', encoding='utf-8') as f:
        f.write(content)

    # check changes
    stdout_content = checked_run("hg status -n -m '{}'".
                                 format(rst_file))

    if stdout_content.strip() == '':
        raise RuntimeError("Nothing to commit and generate")

    # regen docs
    checked_run("./generate.sh", redirect_stdout=False)

    # commit
    checked_run("hg commit --noninteractive -u '{}' -m '{}' '{}'".format(commit_author,
                                                                         commit_message,
                                                                         rst_file))

    # push
    if app.config.get('ALLOW_PUSH', False):
        checked_run('hg push')


def process_update():
    # clean repo
    checked_run("hg clean --all")

    # pull
    checked_run("hg pull -u")

    # regen docs
    checked_run("./generate.sh", redirect_stdout=False)


@app.route('/_update')
def handle_update_page():
    try:
        process_update()

        logging.info('Succeeded updated')
        flash('Repository updated and regenerated', 'success')
    except Exception as e:
        logging.error(str(e))
        first_line = bleach.clean(str(e).split("\n")[0])
        flash(first_line, 'error')

    return redirect('/')


@app.route('/_viewer/<path:doc_path>')
def handle_viewer_page(doc_path):
    full_path = app.config['DOC_ROOT'] + '/' + doc_path

    if not os.path.isfile(full_path):
        return render_template('notfound.html', doc_file=doc_path)

    if doc_path.endswith('.html'):
        logging.debug('Serving doc page ' + str(doc_path))
        rst_file = find_rst_file(doc_path)
        edit_url = '/_editor/' + doc_path
        return render_template('viewer.html', doc_file=doc_path, rst_file=rst_file,
                               edit_url=edit_url)

    if not doc_path.endswith('.js'):
        logging.debug('Serving asset ' + str(doc_path))
        return send_file(full_path)

    return render_template('notfound.html', doc_file=doc_path)


@app.route('/_editor/<path:doc_path>', methods=['GET', 'POST'])
def handle_editor_page(doc_path):
    rst_file = find_rst_file(doc_path)

    if not rst_file:
        return render_template('notfound.html', doc_file=doc_path)

    rst_path = app.config['DOC_ROOT'] + '/' + rst_file

    view_url = '/_viewer/' + doc_path
    edit_url = '/_editor/' + doc_path

    if not os.path.isfile(rst_path):
        return render_template('notfound.html', doc_file=doc_path)

    if request.method == 'GET':
        logging.debug('Serving editor page ' + str(rst_path))
        with open(rst_path, 'r', encoding='utf-8') as f:
            code = f.read()
        commit_author = session.get('hg_author', '')

        return render_template('editor.html', doc_file=doc_path, rst_file=rst_file,
                               edit_url=edit_url, view_url=view_url,
                               commit_author=commit_author,
                               code=code)

    if request.method == 'POST':
        content = request.form['editor-content']
        commit_message = request.form['editor-comment']
        commit_author = request.form['editor-author']

        try:
            process_save(content, commit_message, commit_author, rst_path, rst_file)

            logging.info('Succeeded, author {}, message {}'.format(commit_author, commit_message))
            flash('Document saved and regenerated', 'success')
        except Exception as e:
            logging.error(str(e))
            first_line = bleach.clean(str(e).split("\n")[0])
            flash(first_line, 'error')

        return redirect(view_url)


@app.route('/_content/<path:doc_path>')
def handle_content_page(doc_path):
    full_path = app.config['DOC_ROOT'] + '/' + doc_path

    if not os.path.isfile(full_path):
        return "", 404

    return send_file(full_path)
