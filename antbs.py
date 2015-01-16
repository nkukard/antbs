#!/usr/bin/python
# -*- coding: utf-8 -*-
#
# build_pkg.py
#
# Copyright 2013 Antergos
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston,
# MA 02110-1301, USA.

"""Import the main class from the "Flask" library that we need to create our
web application, as well as the `render_template` function for returning
HTML files as responses and getting `request` objects that contain
information about requests that we receive, like the URL and cookies and
stuff like that."""

import json
import subprocess
import os
import glob
import shutil
from datetime import datetime, timedelta
import ipaddress
from rq import Queue, Connection, Worker
from flask import Flask, request, Response, abort, render_template, url_for, redirect
from werkzeug.contrib.fixers import ProxyFix
import requests
import docker
from flask.ext.stormpath import StormpathManager, groups_required, login_required
from flask.ext.stormpath import user
import gevent
import gevent.monkey
import src.pagination
import src.build_pkg as builder
from src.redis_connection import db
import src.logging_config as logconf
import src.package as package
#import newrelic

gevent.monkey.patch_all()

SRC_DIR = os.path.dirname(__file__) or '.'
STAGING_REPO = '/srv/antergos.info/repo/iso/testing/uefi/antergos-staging'
MAIN_REPO = '/srv/antergos.info/repo/antergos'
STAGING_64 = os.path.join(STAGING_REPO, 'x86_64')
STAGING_32 = os.path.join(STAGING_REPO, 'i686')
MAIN_64 = os.path.join(MAIN_REPO, 'x86_64')
MAIN_32 = os.path.join(MAIN_REPO, 'i686')


# Create the variable `app` which is an instance of the Flask class that
# we just imported.
app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('STORMPATH_SESSION_KEY')
app.config['STORMPATH_API_KEY_ID'] = os.environ.get('STORMPATH_API_KEY_ID')
app.config['STORMPATH_API_KEY_SECRET'] = os.environ.get('STORMPATH_API_KEY_SECRET')
app.config['STORMPATH_APPLICATION'] = os.environ.get('STORMPATH_APPLICATION')
app.config['STORMPATH_ENABLE_USERNAME'] = True
app.config['STORMPATH_REQUIRE_USERNAME'] = True
app.config['STORMPATH_ENABLE_REGISTRATION'] = False
app.config['STORMPATH_REDIRECT_URL'] = '/pkg_review'
app.config['STORMPATH_LOGIN_TEMPLATE'] = 'login.html'
app.config['STORMPATH_COOKIE_DURATION'] = timedelta(days=14)
stormpath_manager = StormpathManager(app)
app.jinja_options = Flask.jinja_options.copy()
app.jinja_options['lstrip_blocks'] = True
app.jinja_options['trim_blocks'] = True

#settings = newrelic.agent.global_settings()
#settings.app_name = 'AntBS'

# Use gunicorn to proxy with nginx
app.wsgi_app = ProxyFix(app.wsgi_app)

# Setup logging
logger = logconf.logger


def copy(src, dst):
    if os.path.islink(src):
        linkto = os.readlink(src)
        os.symlink(linkto, dst)
    else:
        try:
            shutil.copy(src, dst)
        except shutil.SameFileError:
            pass
        except shutil.Error:
            pass


def remove(src):
    if os.path.isdir(src):
        try:
            shutil.rmtree(src)
        except Exception as err:
            logger.error(err)
            return True
    elif os.path.isfile(src):
        try:
            os.remove(src)
        except Exception as err:
            logger.error(err)
            return True
    else:
        return True


def handle_worker_exception(job, *exc_info):
    # TODO: This needs some thought on how to recover instead of bailing on entire build queue
    doc = docker.Client(base_url='unix://var/run/docker.sock', version='1.12', timeout=10)

    container = db.get('container')
    queue = db.lrange('queue', 0, -1)
    try:
        doc.kill(container)
        doc.remove_container(container)
    except Exception:
        logger.error('Unable to kill container')
    repo = os.path.join("/tmp", "staging")
    cache = os.path.join("/tmp", "pkg_cache")

    remove(repo)
    remove(cache)
    remove('/opt/antergos-packages')

    db.set('idle', "True")
    db.set('building', 'Idle')
    db.set('now_building' '')
    db.set('container', '')
    db.set('building_num', '')
    db.set('building_start', '')
    if queue:
        for p in queue:
            db.rpop('queue')

    return True


with Connection(db):
    queue = Queue('build_queue')
    w = Worker([queue], exc_handler=handle_worker_exception)


# def stream_template(template_name, **context):
# app.update_template_context(context)
# t = app.jinja_env.get_template(template_name)
# rv = t.stream(context)
#     # rv.enable_buffering(5)
#     rv.disable_buffering()
#     return rv

def url_for_other_page(page):
    args = request.view_args.copy()
    args['page'] = page
    return url_for(request.endpoint, **args)


app.jinja_env.globals['url_for_other_page'] = url_for_other_page


def get_live_build_ouput():
    psub = db.pubsub()
    psub.subscribe('build-output')
    first_run = True
    while True:
        message = psub.get_message()
        if message:
            if first_run and (message['data'] == '1' or message['data'] == 1):
                message['data'] = db.get('build_log_last_line')
                first_run = False
            elif message['data'] == '1' or message['data'] == 1:
                message['data'] = '...'

            yield 'data: %s\n\n' % message['data']

        gevent.sleep(.05)


def get_paginated(item_list, per_page, page, timeline):
    page -= 1
    if not timeline:
        item_list.reverse()
    paginated = [item_list[i:i + per_page] for i in range(0, len(item_list), per_page)]
    this_page = paginated[page]
    all_pages = len(paginated)

    return this_page, all_pages


def get_build_info(page=None, status=None, logged_in=False, review=False):
    # TODO: I don't like this. Need to come up with something better.
    if page is None or status is None:
        abort(500)
    pkg_info_cache = not db.exists('pkg_info_cache:%s:%s' % (status, page))
    rev_info_cache = not db.exists('rev_info_cache:%s' % page) and review and logged_in
    pending_rev_cache = not db.exists('pending_rev_cache:%s' % page) and review and logged_in
    logger.info('[GET_BUILD_INFO] - CALLED')
    #if any(i for i in (pkg_info_cache, rev_info_cache, pending_rev_cache)):
    if True:
        logger.info('[GET_BUILD_INFO] - "ANY" CONDITION SATISFIED, NOT USING CACHED VALUE')
        try:
            all_builds = db.lrange(status, 0, -1)
        except Exception:
            all_builds = None

        pkg_list = []
        rev_pending = []

        if all_builds is not None:
            builds, all_pages = get_paginated(all_builds, 10, page, False)
            logger.info('@@-antbs.py-@@ [completed route] | all_pages is %s' % all_pages)
            for build in builds:
                try:
                    pkg = db.get('build_log:%s:pkg' % build)
                except Exception:
                    continue
                name = db.get('pkg:%s:name' % pkg)
                bnum = build
                version = db.get('build_log:%s:version' % bnum)
                if not version or version is None:
                    version = db.get('pkg:%s:version' % pkg)
                start = db.get('build_log:%s:start' % bnum)
                end = db.get('build_log:%s:end' % bnum)
                review_stat = db.get('build_log:%s:review_stat' % bnum)
                review_stat = db.get('review_stat:%s:string' % review_stat)
                review_dev = db.get('build_log:%s:review_dev' % bnum)
                review_date = db.get('build_log:%s:review_date' % bnum)
                if logged_in and review and review_stat != "pending":
                    continue
                all_info = dict(bnum=bnum, name=name, version=version, start=start, end=end,
                                review_stat=review_stat, review_dev=review_dev, review_date=review_date)
                pkg_info = {bnum: all_info}
                pkg_list.append(pkg_info)
                db.hset('pkg_info_cache:%s:%s:hash' % (status, page), bnum, all_info)
                #db.rpush('pkg_info_cache:%s:list:%s' % (status, page), pkg_info)
                if logged_in and review and review_stat == "pending":
                    rev_pending.append(pkg_info)
                    db.hset('pending_rev_cache:%s:hash' % page, bnum, all_info)
                    #db.rpush('pending_rev_cache:list:%s' % page, pkg_info)
                else:
                    continue

            if review:
                db.setex('pending_rev_cache:%s' % page, 10800, "True")

            db.setex('rev_info_cache:%s' % page, 10800, "True")
            db.setex('pkg_info_cache:%s:%s' % (status, page), 10800, "True")
            db.setex('all_pages_cache', 10800, all_pages)
    # else:
    #     logger.info('[GET_BUILD_INFO] - "NOT ANY" CONDITION NOT SATISFIED - WE ARE USING CACHED VALUES')
    #     if review:
    #         pkg_list = db.hgetall('pending_rev_cache:%s:hash' % page)
    #     else:
    #         pkg_list = db.hgetall('pkg_info_cache:%s:%s:hash' % (status, page))
    #         logger.info('@@-antbs-py-@@ [GET BUILD INFO] | pkg_list hash is %s' % pkg_list)
    #     rev_pending = db.hgetall('pending_rev_cache:%s:hash' % page)
    #     all_pages = db.get('all_pages_cache')
    #     pkg_list_rebuilt = []
    #     rev_pending_rebuilt = []
    #     for k, v in pkg_list.iteritems():
    #         pkg_list_rebuilt.append(v)
    #         logger.info('@@-antbs-py-@@ [GET BUILD INFO] | pkg_list_rebuilt is %s' % v)
    #
    #     pkg_list = pkg_list_rebuilt
    #     rev_pending = rev_pending_rebuilt

    return pkg_list, int(all_pages), rev_pending


def redirect_url(default='homepage'):
    return request.args.get('next') or request.referrer or url_for(default)


def set_pkg_review_result(bnum=None, dev=None, result=None):
    # TODO: This is garbage. Needs rewrite.
    if any(i is None for i in (bnum, dev, result)):
        abort(500)
    errmsg = dict(error=False, msg=None)
    dt = datetime.now().strftime("%m/%d/%Y %I:%M%p")
    try:
        db.set('build_log:%s:review_stat' % bnum, result)
        if result == "4":
            return errmsg
        db.set('build_log:%s:review_dev' % bnum, dev)
        db.set('build_log:%s:review_date' % bnum, dt)
        db.set('idle', 'False')
        result = int(result)
        pkg = db.get('build_log:%s:pkg' % bnum)
        logger.info('Updating pkg review status for %s.' % pkg)
        logger.info('[UPDATE REPO]: pkg is %s' % pkg)
        logger.info('[UPDATE REPO]: STAGING_64 is %s' % STAGING_64)
        pkg_files_64 = glob.glob('%s/%s-***' % (STAGING_64, pkg))
        pkg_files_32 = glob.glob('%s/%s-***' % (STAGING_32, pkg))
        pkg_files = pkg_files_64 + pkg_files_32
        logger.info('[UPDATE REPO]: pkg_files is %s' % pkg_files)
        logger.info('[PKG_FILES]:')
        if pkg_files and pkg_files is not None:
            logger.info(pkg_files)
            logger.info('Moving %s from staging to main repo.' % pkg)

            for f in pkg_files_64:
                if result is 2 or result == '2':
                    copy(f, MAIN_64)
                    copy(f, '/tmp')
                elif result is 3 or result == '3':
                    os.remove(f)
            for f in pkg_files_32:
                if result is 2 or result == '2':
                    copy(f, MAIN_32)
                    copy(f, '/tmp')
                elif result is 3 or result == '3':
                    os.remove(f)
            if result and result is not 4 and result != '4':
                queue.enqueue_call(builder.update_main_repo, args=(pkg, str(result)), timeout=9600)

        else:
            logger.error('@@-antbs.py-@@ | While moving to main, no packages were found to move.')
            err = 'While moving to main, no packages were found to move.'
            errmsg = dict(error=True, msg=err)

    except (OSError, Exception) as err:
        logger.error('@@-antbs.py-@@ | Error while moving to main: ' + err)
        err = str(err)
        errmsg = dict(error=True, msg=err)

    return errmsg


def get_timeline(tlpage=None):
    event_ids = db.lrange('timeline:all', 0, -1)
    timeline = []
    for event_id in event_ids:
        date = db.get('timeline:%s:date' % event_id)
        time = db.get('timeline:%s:time' % event_id)
        msg = db.get('timeline:%s:msg' % event_id)
        tltype = db.get('timeline:%s:type' % event_id)
        allinfo = dict(event_id=event_id, date=date, msg=msg, time=time, tltype=tltype)
        event = {event_id: allinfo}
        timeline.append(event)
    this_page, all_pages = get_paginated(timeline, 6, tlpage, True)

    return this_page, all_pages


@app.errorhandler(404)
def page_not_found(e):
    return render_template('404.html'), 404


@app.errorhandler(500)
def internal_error(e):
    logger.error(e)
    return render_template('500.html'), 500


@app.errorhandler(400)
def flask_error(e):
    logger.error(e)
    return render_template('500.html'), 400


@app.errorhandler(Exception)
def unhandled_exception(e):
    logger.info(e.message)
    return render_template('500.html'), 500


@app.route("/timeline/<int:tlpage>")
@app.route("/")
def homepage(tlpage=None):
    if tlpage is None:
        tlpage = 1
    is_idle = db.get('idle')
    check_stats = ['queue', 'completed', 'failed']
    building = db.get('building')
    this_page, all_pages = get_timeline(tlpage)
    #logger.info('@@-antbs.py-@@ | this_page is %s' % this_page)
    stats = {}
    for stat in check_stats:
        res = db.llen(stat)
        if stat is not "queue":
            builds = db.lrange(stat, 0, -1)
            within = []
            nodup = []
            for build in builds:
                end = db.get('build_log:%s:start' % build)
                end_fmt = datetime.strptime(end, '%m/%d/%Y %I:%M%p')
                ver = db.get('build_log:%s:version' % build)
                name = db.get('build_log:%s:pkg' % build)
                ver = '%s:%s' % (name, ver)
                if (datetime.now() - end_fmt) < timedelta(hours=48) and ver not in nodup:
                    within.append(build)
                    nodup.append(ver)

            stats[stat] = len(within)
        else:
            stats[stat] = res

    repos = ['main', 'staging']
    x86_64 = None
    cached = None
    for repo in repos:
        if repo == 'main':
            x86_64 = glob.glob('/srv/antergos.info/repo/antergos/x86_64/*.pkg.tar.xz')
            cached = db.exists('repo-count-main')
            if cached and cached is not None:
                stats['repo_main'] = db.get('repo-count-main')
        elif repo == 'staging':
            x86_64 = glob.glob('/srv/antergos.info/repo/iso/testing/uefi/antergos-staging/x86_64/*.pkg.tar.xz')
            cached = db.exists('repo-count-staging')
            if cached and cached is not None:
                stats['repo_staging'] = db.get('repo-count-staging')

        all_p = x86_64
        filtered = []

        if not cached or cached is None:
            for fp in all_p:
                new_fp = os.path.basename(fp)
                if 'dummy-package' not in new_fp:
                    filtered.append(new_fp)
            stats['repo_' + repo] = len(set(filtered))
            db.setex('repo-count-%s' % repo, 1800, stats['repo_' + repo])

    return render_template("overview.html", idle=is_idle, stats=stats, user=user, building=building,
                           this_page=this_page, all_pages=all_pages, page=tlpage)


@app.route("/building")
def build():
    is_idle = db.get('idle')
    now_building = db.get('now_building')
    cont = db.get('container')
    if cont:
        container = cont[:20]
    else:
        container = None
    bnum = db.get('building_num')
    start = db.get('building_start')
    ver = db.get('build_log:%s:version' % bnum)

    return render_template("building.html", idle=is_idle, building=now_building, container=container, bnum=bnum,
                           start=start, ver=ver)
    # return Response(stream_template('building.html', idle=is_idle, bnum=bnum, start=start, building=now_building,
    #                                container=container))


@app.route('/get_log')
def get_log():
    is_idle = db.get('idle')
    if is_idle == "True":
        abort(404)

    return Response(get_live_build_ouput(), direct_passthrough=True, mimetype='text/event-stream')


@app.route('/hook', methods=['POST', 'GET'])
def hooked():
    is_phab = False
    db.set('isPhab', "False")
    repo = None
    if request.method == 'GET':
        return ' Nothing to see here, move along ...'

    elif request.method == 'POST':
        # Check if the POST request if from github.com
        phab = int(request.args.get('phab', '0'))
        if phab and phab > 0:
            is_phab = True
        else:
            # Store the IP address blocks that github uses for hook requests.
            hook_blocks = requests.get('https://api.github.com/meta').json()['hooks']
            for block in hook_blocks:
                ip = ipaddress.ip_address(u'%s' % request.remote_addr)
                if ipaddress.ip_address(ip) in ipaddress.ip_network(block):
                    break  # the remote_addr is within the network range of github
            else:
                abort(403)
            if request.headers.get('X-GitHub-Event') == "ping":
                return json.dumps({'msg': 'Hi!'})
            if request.headers.get('X-GitHub-Event') != "push":
                return json.dumps({'msg': "wrong event type"})
    changes = []
    the_queue = db.lrange('queue', 0, -1)
    building = db.get('now_building')
    if is_phab and request.args['repo'] != "CN":
        repo = 'antergos-packages'
        db.set('pullFrom', 'antergos')
        match = None
        nx_pkg = None
        if request.args['repo'] == "NX":
            nx_pkg = ['numix-icon-theme']
        elif request.args['repo'] == "NXSQ":
            nx_pkg = ['numix-icon-theme-square']
        if nx_pkg:
            if not the_queue or (the_queue and nx_pkg[0] not in the_queue):
                for p in the_queue:
                    if p == nx_pkg[0] or p == building:
                        match = True
                        break
                    else:
                        continue
                if match is None:
                    changes.append(nx_pkg)
                else:
                    logger.info('RATE LIMIT IN EFFECT FOR %s' % nx_pkg[0])
                    return json.dumps({'msg': 'RATE LIMIT IN EFFECT FOR %s' % nx_pkg[0]})
        else:
            logger.error('phab hook failed for numix')

    elif is_phab and request.args['repo'] == "CN":
        db.set('isPhab', "True")
        idle = db.get('idle')
        repo = 'antergos-packages'
        db.set('pullFrom', 'antergos')
        cnchi = ['cnchi-dev']
        changes.append(cnchi)
        working = db.exists('creating-cnchi-archive-from-dev')
        if not working and 'cnchi-dev' not in the_queue and ('cnchi-dev' != building or idle == "True"):
            db.set('creating-cnchi-archive-from-dev', 'True')
            cnchi_git = '/var/repo/CN'
            cnchi_clone = '/tmp/cnchi'
            git = '/tmp/cnchi/.git'
            cnchi_tar_tmp = '/tmp/cnchi.tar'
            cnchi_tar = '/srv/antergos.org/cnchi.tar'

            for f in [cnchi_clone, cnchi_tar, cnchi_tar_tmp]:
                if os.path.exists(f):
                    remove(f)
            try:
                subprocess.check_call(['git', 'clone', cnchi_git, 'cnchi'], cwd='/tmp')

                shutil.rmtree(git)

                subprocess.check_call(['tar', '-cf', '/tmp/cnchi.tar', '-C', '/tmp', 'cnchi'])
                shutil.copy('/tmp/cnchi.tar', '/srv/antergos.org/')
            except subprocess.CalledProcessError as err:
                logger.error(err.output)

            db.delete('creating-cnchi-archive-from-dev')
    else:
        payload = json.loads(request.data)
        full_name = payload['repository']['full_name']
        if 'lots0logs' in full_name and 'antergos/' not in full_name:
            db.set('pullFrom', 'lots0logs')
        else:
            db.set('pullFrom', 'antergos')
        repo = payload['repository']['name']
        pusher = payload['pusher']['name']
        commits = payload['commits']
        if pusher != "antbs":
            for commit in commits:
                changes.append(commit['modified'])
                changes.append(commit['added'])

    if repo == "antergos-packages":
        db.set('idle', 'False')
        logger.info("Build hook triggered. Updating build queue.")
        logger.info(changes)
        has_pkgs = False
        no_dups = []

        for changed in changes:
            for item in changed:
                if is_phab:
                    pak = item
                else:
                    pak = os.path.dirname(item)
                if pak is not None and pak != '' and pak != []:
                    logger.info('Adding %s to the build queue' % pak)
                    no_dups.append(pak)
                    has_pkgs = True

        if has_pkgs:
            the_pkgs = list(set(no_dups))
            first = True
            last = False
            last_pkg = the_pkgs[-1]
            p_ul = []
            if len(the_pkgs) > 1:
                p_ul.append('<ul>')
            for p in the_pkgs:
                if p not in the_queue and p is not None and p != '' and p != []:
                    db.rpush('queue', p)
                    if len(the_pkgs) > 1:
                        p_li = '<li>%s</li>' % p
                    else:
                        p_li = '<strong>%s</strong>' % p
                    p_ul.append(p_li)
                if p == last_pkg:
                    last = True
                queue.enqueue_call(builder.handle_hook, args=(first, last), timeout=9600)
                if last:
                    if is_phab:
                        source = 'Phabricator'
                        tltype = 2
                    else:
                        source = 'Github'
                        tltype = 1
                    if len(the_pkgs) > 1:
                        p_ul.append('</ul>')
                    the_pkgs_str = ''.join(p_ul)
                    tl_event = logconf.new_timeline_event('Webhook triggered by <strong>%s.</strong> Packages added to'
                                                          ' the build queue: %s' % (source, the_pkgs_str), tltype)
                    p_obj = package.Package(p, db)
                    p_obj.save_to_db('tl_event', tl_event)
                first = False

    elif repo == "antergos-iso":
        last = db.get('pkg:antergos-iso:last_commit')
        #if not last or last is None or last == '0':
        if db.get('isoBuilding') == 'False':
            db.set('idle', 'False')
            logger.info("New commit detected. Adding package to queue...")
            db.set('isoFlag', 'True')
            db.rpush('queue', 'antergos-iso')
            #db.setex('pkg:antergos-iso:last_commit', 3600, 'True')
            queue.enqueue_call(builder.handle_hook, timeout=10000)
            logconf.new_timeline_event('<strong>antergos-iso</strong> was added to the <strong>build queue.</strong>')
        else:
            logger.info('RATE LIMIT ON ANTERGOS ISO IN EFFECT')

    return json.dumps({'msg': 'OK!'})


@app.route('/scheduled')
def scheduled():
    is_idle = db.get('idle')
    try:
        pkgs = db.lrange('queue', 0, -1)
    except Exception:
        pkgs = None
    building = db.get('building')
    the_queue = {}
    if pkgs is not None:
        for pak in pkgs:
            name = db.get('pkg:%s:name' % pak)
            version = db.get('pkg:%s:version' % pak)
            all_info = dict(name=name, version=version)
            the_queue[pak] = all_info

    return render_template("scheduled.html", idle=is_idle, building=building, queue=the_queue, user=user)


@app.route('/completed/<int:page>')
@app.route('/completed')
def completed(page=None):
    is_idle = db.get('idle')
    status = 'completed'
    is_logged_in = user.is_authenticated()
    if page is None:
        page = 1
    building = db.get('building')
    completed, all_pages, rev_pending = get_build_info(page, status, is_logged_in)
    logger.info('@@-antbs.py-@@ [completed route] | %s' % all_pages)
    pagination = src.pagination.Pagination(page, 10, all_pages)
    logger.info('@@-antbs.py-@@ [completed route] | %s, %s, %s' % (
        pagination.page, pagination.per_page, pagination.total_count))

    return render_template("completed.html", idle=is_idle, building=building, completed=completed, all_pages=all_pages,
                           rev_pending=rev_pending, user=user, pagination=pagination)


@app.route('/failed/<int:page>')
@app.route('/failed')
def failed(page=None):
    is_idle = db.get('idle')
    status = 'failed'
    if page is None:
        page = 1
    building = db.get('building')
    is_logged_in = user.is_authenticated()

    failed, all_pages, rev_pending = get_build_info(page, status, is_logged_in)

    return render_template("failed.html", idle=is_idle, building=building, failed=failed, all_pages=all_pages,
                           page=page, rev_pending=rev_pending, user=user)


@app.route('/build/<int:num>')
def build_info(num):
    if not num:
        abort(404)
    pkg = db.get('build_log:%s:pkg' % num)
    if not pkg:
        abort(404)
    ver = db.get('pkg:%s:version' % pkg)
    res = db.get('build_log:%s:result' % num)
    start = db.get('build_log:%s:start' % num)
    end = db.get('build_log:%s:end' % num)
    bnum = num
    cont = db.get('container')
    check_log = db.hexists('build_log:%s:content' % bnum, 'content')
    if not check_log:
        log = db.get('build_log:%s:content' % bnum)
    else:
        log = db.hget('build_log:%s:content' % bnum, 'content')
    if log is None or log == '':
        log = 'Unavailable'
    log = log.decode("utf8")
    if cont:
        container = cont[:20]
    else:
        container = None

    return render_template("build_info.html", pkg=pkg, ver=ver, res=res, start=start, end=end,
                           bnum=bnum, container=container, log=log, user=user)


@app.route('/browse/<goto>')
@app.route('/browse')
def repo_browser(goto=None):
    is_idle = db.get('idle')
    building = db.get('building')
    release = False
    testing = False
    main = False
    template = "repo_browser.html"
    if goto == 'release':
        release = True
    elif goto == 'testing':
        testing = True
    elif goto == 'main':
        main = True
        template = "repo_browser_main.html"

    return render_template(template, idle=is_idle, building=building, release=release, testing=testing,
                           main=main, user=user)


@app.route('/pkg_review/<int:page>')
@app.route('/pkg_review', methods=['POST', 'GET'])
@groups_required(['admin'])
def dev_pkg_check(page=None):
    is_idle = db.get('idle')
    status = 'completed'
    set_rev_error = False
    set_rev_error_msg = None
    review = True
    is_logged_in = user.is_authenticated()
    if page is None:
        page = 1
    if request.method == 'POST':
        payload = json.loads(request.data)
        bnum = payload['bnum']
        dev = payload['dev']
        result = payload['result']
        if all(i is not None for i in (bnum, dev, result)):
            set_review = set_pkg_review_result(bnum, dev, result)
            if set_review.get('error'):
                set_rev_error = set_review.get('msg')
                message = dict(error=set_rev_error)
                return json.dumps(message)
            else:
                message = dict(msg='ok')
                db.delete('rev_info_cache')
                db.delete('pkg_info_cache:completed')
                db.delete('pending_rev_cache')
                return json.dumps(message)

    completed, all_pages, rev_pending = get_build_info(page, status, is_logged_in, True)
    pagination = src.pagination.Pagination(page, 10, len(rev_pending))
    return render_template("pkg_review.html", idle=is_idle, completed=completed, all_pages=all_pages,
                           set_rev_error=set_rev_error, set_rev_error_msg=set_rev_error_msg, user=user,
                           rev_pending=rev_pending, pagination=pagination)


@app.route('/build_pkg_now', methods=['POST', 'GET'])
@groups_required(['admin'])
def build_pkg_now():
    if request.method == 'POST':
        pkgname = request.form['pkgname']
        dev = request.form['dev']
        if not pkgname or pkgname is None or pkgname == '':
            abort(500)
        args = (True, True)
        if 'antergos-iso' == pkgname:
            if db.get('isoBuilding') == 'False':
                db.set('isoFlag', 'True')
                args = (True, True)
            else:
                logger.info('RATE LIMIT ON ANTERGOS ISO IN EFFECT')
                return redirect(redirect_url())

        db.rpush('queue', pkgname)
        queue.enqueue_call(builder.handle_hook, args=args, timeout=9600)
        logconf.new_timeline_event(
            '<strong>%s</strong> added %s to the <strong>build queue.</strong>' % (dev, pkgname ))

    return redirect(redirect_url())


@app.route('/get_status', methods=['GET'])
def get_status():
    idle = db.get('idle')
    building = db.get('building')
    if idle == 'True':
        message = dict(msg='Idle')
    else:
        message = dict(msg=building)

    return json.dumps(message)


# Some boilerplate code that just says "if you're running this from the command
# line, start here." It's not critical to know what this means yet.
if __name__ == "__main__":
    app.debug = True
    app.run(port=8020)
