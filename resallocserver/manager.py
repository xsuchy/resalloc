# Central brain of the resalloc server.
# -*- coding: utf-8 -*-
# Copyright (C) 2017 Red Hat, Inc.
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
# You should have received a copy of the GNU General Public License along
# with this program; if not, write to the Free Software Foundation, Inc.,
# 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA.

import base64
import os
import errno
import time
import threading
import subprocess
import warnings
from datetime import datetime
from resalloc import helpers
from resalloc.helpers import RState
from resallocserver import models
from resallocserver.app import session_scope, app
from resallocserver.logic import (
        QResources, QTickets, assign_ticket, release_resource
)
from resallocserver.priority_queue import PriorityQueue, PriorityQueueTask


REUSED_RESOURCE_PRIORITY = 500


def command_env(pool_id=None, res_id=None, res_name=None,
                id_in_pool=None, data=None):
    pfx = 'RESALLOC_'
    env = os.environ.copy()
    env[pfx + 'ID'] = str(res_id)
    env[pfx + 'NAME'] = str(res_name)
    env[pfx + 'POOL_ID'] = str(pool_id)
    env[pfx + 'ID_IN_POOL'] = str(id_in_pool)
    if data is not None:
        env[pfx + 'RESOURCE_DATA'] = base64.b64encode(data)
    return env


def run_command(pool_id, res_id, res_name, id_in_pool, command, ltype='alloc',
                catch_stdout_bytes=None, data=None,
                catch_stdout_lines_securely=False):
    app.log.debug("running: " + command)
    env = command_env(pool_id, res_id, res_name, id_in_pool, data)
    config = app.config

    ldir = os.path.join(config['logdir'], 'hooks')
    try:
        os.mkdir(ldir)
    except OSError as e:
        if e.errno != errno.EEXIST:
            raise

    lfile = os.path.join(ldir, '{0:06d}_{1}'.format(res_id, ltype))
    with open(lfile, 'a+b') as logfile:

        if not catch_stdout_bytes:
            return {'status': subprocess.call(command, env=env, shell=True,
                                              stdout=logfile, stderr=logfile)}

        stdout_written = 0
        stdout_stopped = False

        # Run the sub-command to be captured.
        sp = subprocess.Popen(command, env=env, shell=True,
                              stdout=subprocess.PIPE, stderr=logfile)

        captured_string = b""


        for line in iter(sp.stdout.readline, b''):
            # Write to the log.
            logfile.write(line)
            logfile.flush()

            if stdout_stopped:
                continue

            if stdout_written + len(line) > catch_stdout_bytes:
                if stdout_written == 0 and not catch_stdout_lines_securely:
                    # Even the first line is too long for this buffer.  Catch at
                    # least part of it.
                    line = line[:catch_stdout_bytes]
                    captured_string += line

                stdout_stopped = True
                if not catch_stdout_lines_securely:
                    captured_string += b"<< trimmed >>\n"
                continue

            stdout_written += len(line)
            captured_string += line


    return {
        'status': sp.wait(),
        'stdout': captured_string,
    }


def reload_config():
    config_dir = app.config["config_dir"]
    config_file = os.path.join(config_dir, "pools.yaml")
    config = helpers.load_config_file(config_file)

    pools = {}
    for pool_id in config:
        assert not pool_id in pools
        pool = Pool(pool_id)
        pool.from_dict(config[pool_id])
        pool.validate()
        pools[pool_id] = pool

    return pools


class ThreadLocalData(threading.local):
    """
    Object of threading.local is always empty right after t.start() is called.
    But if we define __init__() here, the method is always called appropriately
    for each thread (so we have a convenient way to initialize thread-local
    data).

    This trick is described in/taken from  `_threading_local.py` (cpython).
    """
    def __init__(self, **kw):
        self.__dict__.update(kw)
        super(ThreadLocalData, self).__init__()


class Worker(threading.Thread):
    def __init__(self, event, pool, res_id, name=None):
        self.local = ThreadLocalData(
            pool=pool,
            resource_id=res_id,
        )
        if name is not None:
            name = "{0}-{1}".format(name, res_id or pool)
        self.event = event
        threading.Thread.__init__(self, name=name)

    def job(self):
        """ The task to be done by background thread. """
        raise NotImplementedError

    def __getattr__(self, attr):
        return getattr(self.local, attr)

    def run(self):
        self.log = app.log.getChild("worker")
        try:
            self.job()
        except:
            self.log.exception("Worker exception, pool=%s resource=%s",
                               self.pool, self.resource_id)
            raise


class TerminateWorker(Worker):
    def __init__(self, event, pool, res_id):
        super(TerminateWorker, self).__init__(event, pool, res_id, "Terminator")

    def close(self):
        with session_scope() as session:
            resource = session.query(models.Resource).get(self.resource_id)
            resource.state = RState.ENDED
            session.add(resource)
            if resource.id_in_pool_object:
                session.delete(resource.id_in_pool_object)
        self.event.set()

    def job(self):
        id_in_pool = None
        with session_scope() as session:
            resource = session.query(models.Resource).get(self.resource_id)
            if resource.ticket:
                if resource.ticket.state == helpers.TState.OPEN:
                    self.log.warning("can't delete {0}, ticket opened"\
                                     .format(resource.name))
                    return
            resource.state = RState.DELETING
            session.add(resource)
            session.flush()
            id_in_pool = resource.id_in_pool
            session.expunge(resource)

        self.log.info("Terminating %s started", resource.name)
        if not self.pool.cmd_delete:
            self.close()
            return

        run_command(
                self.pool.id,
                resource.id,
                resource.name,
                id_in_pool,
                self.pool.cmd_delete,
                'terminate',
                data=resource.data,
        )
        self.close()
        self.log.info("Terminating %s finished", resource.name)


class ReleaseWorker(Worker):
    def __init__(self, event, pool, res_id):
        super(ReleaseWorker, self).__init__(event, pool, res_id, "Releaser")

    """ Call `Pool.cmd_release` shell command asynchronously """
    def job(self):
        with session_scope() as session:
            resource = session.query(models.Resource).get(self.resource_id)
            id_in_pool = resource.id_in_pool
            resource_name = resource.name
            session.expunge(resource)

        self.log.info("Releasing %s", resource_name)
        out = run_command(self.pool.id, resource.id, resource_name, id_in_pool,
                          self.pool.cmd_release, "release", data=resource.data)
        status = out["status"]

        with session_scope() as session:
            # re-query the expunged resource
            mod_resource = session.query(models.Resource).get(self.resource_id)
            if status:
                self.log.error("Releasing worker failed: pool=%s name=%s cmd=%s",
                               self.pool.name, resource_name,
                               self.pool.cmd_release)
                # mark it for removal
                mod_resource.releases_counter = self.pool.reuse_max_count + 1
            mod_resource.state = RState.UP

        if not status:
            self.event.set()

        self.log.info("Releasing %s finished", resource_name)


class AllocWorker(Worker):
    def __init__(self, event, pool, res_id, name=None):
        super(AllocWorker, self).__init__(event, pool, res_id, "Allocator")

    def job(self):
        id_in_pool = None
        with session_scope() as session:
            resource = session.query(models.Resource).get(self.resource_id)
            id_in_pool = resource.id_in_pool
            session.expunge(resource)

        self.log.info(
            "Allocating %s (#%s in pool '%s') TID=%s",
            resource.name, id_in_pool, self.pool.name,
            threading.current_thread().ident
        )

        # Run the allocation script.
        output = run_command(
            self.pool.id,
            resource.id,
            resource.name,
            id_in_pool,
            self.pool.cmd_new,
            catch_stdout_bytes=512,
        )

        with session_scope() as session:
            resource.state = RState.ENDED if output['status'] else RState.UP
            resource.data = output['stdout']
            tags = []
            if not isinstance(self.pool.tags, list):
                msg = "Pool {pool} has set 'tags' set, but that's not an array"\
                        .format(pool=self.name)
                warnings.warn(msg)
            else:
                for tag in self.pool.tags:
                    tag_name = None
                    tag_priority = 0
                    if isinstance(tag, str):
                        # older format
                        tag_name = tag
                    elif isinstance(tag, dict):
                        tag_name = tag['name']
                        tag_priority = tag.get('priority', 0)
                    else:
                        assert False

                    tag_obj = models.ResourceTag()
                    tag_obj.id = tag_name
                    tag_obj.resource_id = resource.id
                    tag_obj.priority = tag_priority
                    tags.append(tag_obj)

            self.log.info("Allocating %s finished => %s",
                          resource.name, resource.state)
            session.add_all(tags + [resource])

            if resource.state == RState.ENDED:
                session.delete(resource.id_in_pool_object)


        # Notify manager that it is worth doing re-spin.
        self.event.set()


class CleanUnknownWorker(Worker):
    """
    Delete all resources that are not recognized by resalloc
    https://github.com/praiskup/resalloc/issues/88
    """
    def __init__(self, event, pool, res_id, name=None):
        super(CleanUnknownWorker, self).__init__(event, pool, res_id,
                                                 "GarbageCleaner")

    def job(self):
        self.log.info("Cleaning unused resources in %s pool", self.pool.id)

        all_resources = self._list_all_resources()
        known_resources = self._list_known_resources()
        unknown_resources = set(all_resources) - set(known_resources)

        # There is no assigned resource ID for the given resource, that's
        # the whole point. Let's simply use 0, which means the termination
        # output for all unknown resources will be logged into
        # hooks/000000_terminate
        res_id=0

        for resource in unknown_resources:
            run_command(
                self.pool.id,
                res_id,
                resource,
                None,
                self.pool.cmd_delete,
                'terminate',
                data=None,
            )

    def _list_all_resources(self):
        """
        List all resources using `cmd_list` from `pools.yaml`. This may include
        resources that are not known to resalloc
        """
        result = run_command(
            self.pool.id,
            res_id=0,
            res_name=None,
            id_in_pool=None,
            command=self.pool.cmd_list,
            ltype="list",
            catch_stdout_bytes=5120,
            catch_stdout_lines_securely=True,
        )
        return result["stdout"].decode("utf-8").strip().split()

    def _list_known_resources(self):
        with session_scope() as session:
            on = QResources(session).on().all()
            return [resource.name for resource in on]


class Watcher(threading.Thread):
    def loop(self):
        app.log.info("Watcher loop")
        pools = reload_config()
        to_check = {}
        with session_scope() as session:
            # Even though we never terminate resources that have assigned
            # ticket, we still check them.  This raises the check limit before
            # user releases the ticket and the resource can be terminated as
            # soon as possible.
            up = QResources(session).up().all()
            for item in up:
                if not item.pool in pools:
                    continue
                to_check[item.id] = {
                    'name': item.name,
                    'pool': item.pool,
                    'last': item.check_last_time,
                    'fail': item.check_failed_count,
                    'id_in_pool': item.id_in_pool,
                    'data': item.data,
                }

        for res_id, data in to_check.items():
            pool = pools[data['pool']]
            if not pool.cmd_livecheck:
                continue
            if data['last'] + pool.livecheck_period > time.time():
                # Not yet needed check.
                continue

            rc = run_command(
                    pool.id,
                    res_id,
                    data['name'],
                    data['id_in_pool'],
                    pool.cmd_livecheck,
                    'watch',
                    data=data["data"],
            )

            with session_scope() as session:
                res = session.query(models.Resource).get(res_id)
                res.check_last_time = time.time()
                if rc['status']:
                    res.check_failed_count = res.check_failed_count + 1
                    app.log.info("Check %s fail count %d", res_id,
                                 res.check_failed_count)
                else:
                    res.check_failed_count = 0
                session.add(res)
                session.flush()

    def run(self):
        while True:
            self.loop()
            time.sleep(app.config["sleeptime"] / 2)


class Pool(object):
    max = 4
    max_starting = 1
    max_prealloc = 2
    # Minimal time in seconds to wait between subsequent resource starts.
    # If the resource fails to start, the timeout still applies.  Note, however,
    # that there's no event (yet?) to wake-up manager if this timeout lasts.
    start_delay = 0

    cmd_new = None
    cmd_delete = None
    cmd_livecheck = None
    cmd_release = None
    cmd_list = None
    livecheck_period = 600
    tags = None
    name_pattern = "{pool_name}_{id}_{datetime}"

    reuse_opportunity_time = 0
    reuse_max_count = 0
    reuse_max_time = 3600

    def __init__(self, id):
        self.id = id
        # TODO: drop this
        self.name = id


    def loop(self, event):
        """
        Perform one Pool iteration across all the corresponding instances,
        and adjust the resource/ticket states.  ``event`` is the
        ``Synchronizer().ticket`` object.
        """

        # decouple ticket from resource, and maybe switch UP → RELEASING
        self._detect_closed_tickets(event)

        # switch UP → DELETE_REQUEST
        self._request_resource_removal()

        # switch DELETE_REQUEST → ENDED
        self._garbage_collector(event)

        self._allocate_more_resources(event)

        # Delete all resources that are not recognized by resalloc
        self._clean_unknown_resources(event)


    def validate(self):
        assert(self.cmd_new)
        assert(self.cmd_delete)


    def _allocate_pool_id(self, session, resource):
        # allocate the lowest available pool_id
        ids_query = (
                session.query(models.IDWithinPool)
                       .filter_by(pool_name=self.name)
                       .order_by(models.IDWithinPool.id)
        )
        ids = {x.id: True for x in ids_query.all()}

        found_id = None
        try_id = 0
        while True:
            if try_id in ids:
                try_id += 1
                continue

            found_id = models.IDWithinPool()
            found_id.id = try_id
            found_id.pool_name = self.name
            found_id.resource_id = resource.id
            return found_id


    def allocate(self, event):
        resource_id = None
        with session_scope() as session:
            dbinfo = session.query(models.Pool).get(self.name)
            dbinfo.last_start = time.time()
            resource = models.Resource()
            resource.pool = self.name
            session.add_all([resource, dbinfo])
            session.flush()

            pool_id = self._allocate_pool_id(session, resource)
            session.add(pool_id)
            session.flush()

            resource_id = resource.id
            fill_dict = dict(
                id=str(resource_id).zfill(8),
                pool_name=self.name)
            resource.name = helpers.careful_string_format(
                    self.name_pattern, fill_dict)
            session.add(resource)
        if resource_id:
            self.last_start = time.time()
            AllocWorker(event, self, int(resource_id)).start()

    def from_dict(self, data):
        allowed_types = [int, str, dict, type(None)]

        if type(data) != dict:
            # TODO: warning
            return

        for key in data:
            if not hasattr(self, key):
                warnings.warn("useless config option '{0}'".format(key))
                continue

            local = getattr(self, key)
            conf_type = type(local)
            if not conf_type in allowed_types:
                continue

            if conf_type == dict:
                setattr(self, key, helpers.merge_dict(local, data[key]))
            else:
                setattr(self, key, data[key])

    def _too_soon(self):
        last_start = 0.0
        with session_scope() as session:
            dbinfo = session.query(models.Pool).get(self.name)
            if not dbinfo:
                dbinfo = models.Pool()
                dbinfo.name = self.name
                dbinfo.last_start = 0.0
                session.add(dbinfo)
            else:
                last_start = dbinfo.last_start

        is_too_soon = last_start + self.start_delay > time.time()
        if is_too_soon:
            app.log.debug("Too soon for Pool('%s')", self.name)
        return is_too_soon

    def _allocate_more_resources(self, event):
        while True:
            with session_scope() as session:
                qres = QResources(session, pool=self.name)
                stats = qres.stats()

            msg = "=> POOL('{0}'):".format(self.name)
            for key, val in stats.items():
                msg = msg + ' {0}={1}'.format(key,val)
            app.log.debug(msg)

            if stats['on'] >= self.max \
                   or stats['free'] + stats['start'] >= self.max_prealloc \
                   or stats['start'] >= self.max_starting \
                   or self._too_soon():
                # Quota reached, don't allocate more.
                break

            self.allocate(event)

    def _clean_unknown_resources(self, event):
        if not self.cmd_list:
            return

        with session_scope() as session:
            dbinfo = session.query(models.Pool).get(self.name)
            last_cleanup = dbinfo.cleaning_unknown_resources
            if last_cleanup is None:
                last_cleanup = datetime.min

        delta = datetime.now() - last_cleanup
        minutes = delta.total_seconds() / 60

        # We don't want to clean up that often
        if minutes < 30:
            return

        worker = CleanUnknownWorker(event, self, res_id=None)
        worker.start()

        with session_scope() as session:
            dbinfo.cleaning_unknown_resources = datetime.now()
            session.add(dbinfo)

    def _detect_closed_tickets(self, event):
        close_resources = []

        with session_scope() as session:
            qres = QResources(session, pool=self.name)

            for resource in qres.taken():
                ticket = resource.ticket
                assert ticket
                if ticket.state == helpers.TState.CLOSED:
                    release_resource(ticket)
                    if self.cmd_release:
                        # UP → RELEASING → UP, TODO: we might want to optimize
                        # this a bit, and stop calling the releasing script when
                        # the resource is not releasable anymore (max_reuses
                        # reached, etc.).
                        resource.state = helpers.RState.RELEASING
                        close_resources.append(int(resource.id))

        # We need to call this after the session_scope() above, because the
        # ReleaseWorker itself modifies the Resource records in DB concurrently.
        # Relates:
        # https://pagure.io/copr/copr/issue/2083
        # https://github.com/praiskup/resalloc/pull/87
        for resource_id in close_resources:
            ReleaseWorker(event, self, resource_id).start()

    def _request_resource_removal(self):
        with session_scope() as session:
            now = time.time()
            qres = QResources(session, pool=self.name)

            for res in qres.check_failure_candidates():
                if res.check_failed_count >= 3:
                    app.log.warning(
                        "Requesting %s removal for continuous failures",
                        res.name)
                    res.state = RState.DELETE_REQUEST
                    continue

            for res in qres.clean_candidates():
                if not self.reuse_opportunity_time:
                    # reuse turned off by default, remove no matter what
                    app.log.debug("Removing %s, not reusable", res.name)
                    res.state = RState.DELETE_REQUEST
                    continue

                if res.released_at < (now - self.reuse_opportunity_time):
                    app.log.debug("Removing %s, not taken quickly enough", res.name)
                    res.state = RState.DELETE_REQUEST
                    continue

                if self.reuse_max_time:
                    last_allowed = now - self.reuse_max_time
                    if res.sandboxed_since < last_allowed:
                        app.log.debug(
                            "Requesting %s removal, too long in one sandbox, "
                            "since %s, last_allowed_time was %s, now %s",
                            res.name, res.sandboxed_since, last_allowed, now)
                        res.state = RState.DELETE_REQUEST
                        continue

                if self.reuse_max_count and \
                        res.releases_counter > self.reuse_max_count:
                    app.log.debug(
                        "Requesting %s removal, max_reuses=%d reached",
                        res.name, self.reuse_max_time)
                    res.state = RState.DELETE_REQUEST
                    continue

    def _garbage_collector(self, event):
        to_terminate = []
        with session_scope() as session:
            qres = QResources(session, pool=self.name)
            for res in qres.clean().all():
                TerminateWorker(event, self, int(res.id)).start()


class PrioritizedResource(PriorityQueueTask):
    """
    Resource with priority (calculated from matching tasks).
    """
    def __init__(self, resource):
        self.resource = resource

    @property
    def object_id(self):
        """ Object_id from the resource id """
        return self.resource.id


class Manager(object):
    def __init__(self, sync):
        self.sync = sync

    def _notify_waiting(self, thread_id):
        self.sync.tid = thread_id
        with self.sync.resource_ready:
            self.sync.resource_ready.notify_all()

    def _assign_tickets(self):
        with session_scope() as session:
            qticket = QTickets(session)
            tickets = [x.id for x in qticket.waiting().order_by(models.Ticket.id).all()]

        for ticket_id in tickets:
            notify_ticket = False
            with session_scope() as session:
                ticket = session.query(models.Ticket).get(ticket_id)
                qres = QResources(session)
                resources = qres.ready().all()

                if not resources:
                    app.log.debug("No available resource, skipping %s", ticket)
                    continue

                queue = PriorityQueue()
                ticket_tags = ticket.tag_set
                for resource in resources:
                    res_tags = resource.tag_set
                    if resource.sandbox and resource.sandbox != ticket.sandbox:
                        continue
                    if not ticket_tags.issubset(res_tags):
                        continue

                    priority = 0
                    for tag in resource.tags:
                        if tag.priority is not None and tag.id in ticket_tags:
                            priority += tag.priority

                    if resource.sandbox:
                        # Re-used resources should be preferred to avoid
                        # allocating new and new resources for the same
                        # sandboxes.  TODO, make this configurable once needed.
                        priority += REUSED_RESOURCE_PRIORITY

                    queue.add_task(resource, priority)

                try:
                    resource = queue.pop_task()
                except KeyError:
                    app.log.debug("%d resources UP but unusable for %s",
                                  len(resources), ticket)
                    continue

                # we found an appropriate resource
                app.log.info("Assigning %s to %s", resource.name, ticket.id)
                assign_ticket(resource, ticket)
                if ticket.tid:
                    notify_ticket = ticket.tid

            # notify ticket when the session is closed (to have short sessions)
            if notify_ticket:
                self._notify_waiting(notify_ticket)


    def _loop(self):
        app.log.info("Manager's loop.")

        # Cleanup the old resources.
        for _, pool in reload_config().items():
            pool.loop(self.sync.ticket)

        # Assign tasks.  This needs to be done after _detect_closed_tickets(),
        # because that call potentially releases some resources which need be
        # preferably re-used to not waste resources.
        self._assign_tickets()


    def run(self):
        threading.current_thread().name = "Manager"

        watcher = Watcher(name="Watcher")
        watcher.event = self.sync.ticket
        watcher.daemon = True
        watcher.start()

        self._loop()
        while True:
            # Wait for the request to set the event (or timeout).
            self.sync.ticket.wait(timeout=app.config["sleeptime"])
            # Until the wait() is called again, any additional event.set() call
            # means another round (even though it might do nothing).
            self._loop()
