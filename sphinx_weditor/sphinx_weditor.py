import enum
import logging
import os
import re
import subprocess
import sys
import tempfile
import time
from typing import Pattern, Union
import urllib.parse
from typing import Optional

import bleach
from bs4 import BeautifulSoup
from flask import Flask, render_template, redirect, send_file, request, flash, session
from flask_bootstrap import Bootstrap
from jinja2 import Markup

app = Flask('sphinx_weditor')


class RegenType(enum.Enum):
    HTML = 0,
    PDF = 1


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


def find_matched_by_filename(top_dir: str, find_name: Union[str, Pattern]) -> list:
    result = []
    top_abs_path = os.path.join(app.config['DOC_ROOT'], top_dir)
    if os.path.isdir(top_abs_path):
        for root, dirs, files in os.walk(top_abs_path):
            for name in files:
                if isinstance(find_name, Pattern):
                    matches = find_name.match(name)
                else:
                    matches = find_name == name
                if matches:
                    abs_path = root + '/' + name
                    rel_path = os.path.relpath(abs_path, app.config['DOC_ROOT'])
                    result.append(rel_path)
    return result


def extract_module_name_by_referer(referer: str) -> Optional[str]:
    referer_path = urllib.parse.urlparse(str(referer)).path.split('/')
    while referer_path and referer_path[0] in ['', '_viewer', '_editor', '_pdf']:
        referer_path.pop(0)
    while referer_path and referer_path[-1] in ['index.htm', 'index.html']:
        referer_path.pop()
    if referer_path:
        module_name = referer_path[0]
        if module_name.endswith('.html') or module_name.endswith('.htm'):
            logging.warning('No proper module name at' + str(referer))
            return None
        logging.info("Referer module name is " + module_name)
        return module_name
    logging.warning('No module name at' + str(referer))
    return None


def find_rst_file(doc_path) -> Optional[str]:
    with open(app.config['DOC_ROOT'] + '/' + doc_path, 'r', encoding='utf-8') as fp:
        soup = BeautifulSoup(fp, "html.parser")

    elements = soup.find_all('a')
    elements_with_rst_href = [element for element in elements if
                              element.attrs['href'] and
                              element.attrs['href'].split('/')[-1].endswith('.rst.txt')]

    if len(elements_with_rst_href) != 1:
        return None

    rst_rel = elements_with_rst_href[0].attrs['href']
    logging.debug('Found rel to source ' + str(rst_rel))
    rst_rel = rst_rel.split('/')[-1]
    if rst_rel.endswith('.rst.txt'):
        rst_rel = rst_rel[0:-4]

    logging.debug('Bare name ' + str(rst_rel))

    top_dir = app.config['DOC_SRC']
    rel_pathes = find_matched_by_filename(top_dir, rst_rel)

    if not rel_pathes:
        top_dir = doc_path.split('/')[0]
        logging.debug('Name is not found at doc_src, trying at top-level dir ' + top_dir)
        rel_pathes = find_matched_by_filename(top_dir, rst_rel)

    if not rel_pathes:
        logging.debug('Name is not found')
        return None
    if len(rel_pathes) == 1:
        return rel_pathes[0]
    if len(rel_pathes) > 1:
        raise RuntimeError("RST source '{}' is not unique, cannot decide".format(rst_rel))


def find_pdf_file(module_name) -> Optional[str]:
    module_dir = app.config['DOC_ROOT'] + '/' + module_name
    rel_pathes = find_matched_by_filename(module_dir, re.compile('.*\.pdf$'))

    if not rel_pathes:
        logging.debug('Name is not found')
        return None
    if len(rel_pathes) == 1:
        return rel_pathes[0]
    if len(rel_pathes) > 1:
        raise RuntimeError("Source '{}' is not unique, cannot decide".format(module_name))


@app.route('/')
def handle_root():
    return redirect('/_viewer/index.html')


def checked_run(cmd: str, redirect_stdout: bool = True, error_text: str = None):
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
        if error_text:
            full_error_text = 'Error: ' + error_text
        else:
            full_error_text = "Error at '{}': code {}, out {}".format(cmd,
                                                                      ret.returncode,
                                                                      redirected)
        raise RuntimeError(full_error_text)

    return redirected


def process_save(content, commit_message, commit_author, rst_path, rst_file,
                 module_name: str):
    logging.info("--- Do save")

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

    # now update the repo to see new changes since last update

    # pull
    checked_run("hg pull")

    # update
    checked_run("hg update --tool=internal:fail --noninteractive", error_text='Update conflict')

    # regen docs
    call_regen(module_name, RegenType.HTML)

    # commit
    checked_run("hg commit --noninteractive -u '{}' -m '{}' '{}'".format(commit_author,
                                                                         commit_message,
                                                                         rst_file))

    # push
    if app.config.get('ALLOW_PUSH', False):
        checked_run('hg push')


def process_update(module_name: Optional[str], regen_type: RegenType):
    logging.info("--- Do update")

    # clean modified
    checked_run("hg update -C")

    # clean repo
    checked_run("hg clean --all")

    # pull
    checked_run("hg pull")

    # update
    checked_run("hg update --tool=internal:fail --noninteractive", error_text='Update conflict')

    # regen docs
    call_regen(module_name, regen_type)


def call_regen(module_name: Optional[str], regen_type: RegenType):
    time_start = time.time()
    if regen_type == RegenType.HTML:
        cmd_line = app.config['REGEN_SCRIPT']
    elif regen_type == RegenType.PDF:
        cmd_line = app.config['REGEN_PDF_SCRIPT']
    else:
        cmd_line = None

    if not cmd_line:
        raise RuntimeError('Cannot find script for type {}'.format(regen_type.name))
    if module_name:
        cmd_line += " " + module_name
    checked_run(cmd_line, redirect_stdout=False)
    time_delta = int(time.time() - time_start)
    logging.info("Generating take {} sec".format(time_delta))


def process_cleanup():
    logging.info("--- Do cleanup")

    # clean uncommited files
    checked_run("hg update -C")


def process_autoupdate(module_name: Optional[str], regen_type: RegenType):
    kwargs = dict(shell=True,
                  check=False,
                  cwd=app.config['DOC_ROOT'])
    ret = subprocess.run("hg incoming --limit 1", **kwargs)
    if ret.returncode == 0:
        logging.info("Incoming changes, auto updating")
        flash('Repository autoupdated', 'success')
        process_update(module_name=module_name, regen_type=regen_type)


@app.route('/_update')
def handle_update_page():
    try:
        if app.config.get('MODULES', False):
            module_name = extract_module_name_by_referer(request.referrer)
        else:
            module_name = None

        process_update(module_name, RegenType.HTML)

        logging.info('Succeeded updated')
        flash('Repository updated and regenerated', 'success')
    except Exception as e:
        logging.error(str(e))
        first_line = bleach.clean(str(e).split("\n")[0])
        flash(first_line, 'error')
        # also cleanup
        process_cleanup()

    if request.referrer:
        return redirect(request.referrer)
    else:
        return redirect('/')


@app.route('/_viewer/<path:doc_path>')
def handle_viewer_page(doc_path):
    full_path = app.config['DOC_ROOT'] + '/' + doc_path

    if not os.path.isfile(full_path):
        return render_template('notfound.html', doc_file=doc_path)

    if doc_path.endswith('.html'):
        if app.config.get('MODULES', False):
            module_name = extract_module_name_by_referer('/_viewer/' + doc_path)
        else:
            module_name = None

        process_autoupdate(None, RegenType.HTML)
        logging.debug('Serving doc page ' + str(doc_path))
        rst_file = find_rst_file(doc_path)
        edit_url = '/_editor/' + doc_path
        if module_name:
            pdf_url = '/_pdf/' + module_name
        else:
            pdf_url = None
        return render_template('viewer.html', doc_file=doc_path, rst_file=rst_file,
                               edit_url=edit_url, pdf_url=pdf_url)

    if not doc_path.endswith('.js'):
        # logging.debug('Serving asset ' + str(doc_path))
        return send_file(full_path)

    return render_template('notfound.html', doc_file=doc_path)


@app.route('/_pdf/<path:module_name>')
def handle_pdf_page(module_name):
    if not module_name:
        return render_template('notfound.html', doc_file='/')

    logging.debug('Module is ' + module_name)
    # usual update
    process_autoupdate(module_name, RegenType.HTML)
    # Call PDF regen without cleaning whole bunch of htmls
    call_regen(module_name, RegenType.PDF)
    pdf_file = find_pdf_file(module_name)
    if pdf_file:
        logging.debug('Serving pdf page ' + str(pdf_file))
        full_path = app.config['DOC_ROOT'] + '/' + pdf_file
        return send_file(full_path)

    return render_template('notfound.html', doc_file='/')


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

        if app.config.get('MODULES', False):
            module_name = extract_module_name_by_referer(request.referrer)
        else:
            module_name = None

        try:
            process_save(content, commit_message, commit_author, rst_path, rst_file,
                         module_name)

            logging.info('Succeeded, author {}, message {}'.format(commit_author, commit_message))
            flash('Document saved and regenerated', 'success')
        except Exception as e:
            logging.error(str(e))
            first_line = bleach.clean(str(e).split("\n")[0])
            flash(first_line, 'error')
            # open editor again with the same content
            return render_template('editor.html', doc_file=doc_path, rst_file=rst_file,
                                   edit_url=edit_url, view_url=view_url,
                                   commit_author=commit_author,
                                   code=content)

        return redirect(view_url)


@app.route('/_content/<path:doc_path>')
def handle_content_page(doc_path):
    full_path = app.config['DOC_ROOT'] + '/' + doc_path

    if not os.path.isfile(full_path):
        return "", 404

    return send_file(full_path)


@app.route('/_preview', methods=['POST'])
def handle_preview():
    logging.info("Got _preview call {}".format(len(request.data)))

    with tempfile.TemporaryDirectory() as tmpdirname:
        in_file = os.path.join(tmpdirname, 'in.rst')
        out_file = os.path.join(tmpdirname, 'out.html')

        with open(in_file, 'wb') as f:
            f.write(request.data)

        command = ['pandoc', '-f', 'rst', '-t', 'html5',
                   in_file, '-o', out_file]
        ret = subprocess.run(command, shell=False, check=False,
                             stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        if ret.returncode == 0:
            with open(out_file, 'rb') as f:
                response = f.read().decode('utf-8')
            return response, 200
        else:
            response = ret.stderr.decode('utf-8')
            return response, 400
