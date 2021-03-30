# coding=utf-8

# Copyright (c) 2001-2015, Canal TP and/or its affiliates. All rights reserved.
#
# This file is part of fabric_navitia, the provisioning and deployment tool
#     of Navitia, the software to build cool stuff with public transport.
#
# Hope you'll enjoy and contribute to this project,
#     powered by Canal TP (www.canaltp.fr).
# Help us simplify mobility and open public transport:
#     a non ending quest to the responsive locomotion way of traveling!
#
# LICENCE: This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program. If not, see <http://www.gnu.org/licenses/>.
#
# Stay tuned using
# twitter @navitia
# IRC #navitia on freenode
# https://groups.google.com/d/forum/navitia
# www.navitia.io

from contextlib import contextmanager
from envelopes import Envelope
import functools
from multiprocessing.dummy import Pool as ThreadPool
from fabric.context_managers import settings
import os
from pipes import quote
import random
from retrying import Retrying, RetryError
import string
from threading import Thread
from cStringIO import StringIO
import time
import datetime
import semver
from abc import ABCMeta, abstractmethod
import re

import requests
from requests.auth import HTTPBasicAuth

from fabric.api import env, task, roles, run, put, sudo, warn_only, execute, hide
from fabric.colors import green, yellow, red
from fabric.context_managers import cd
from fabric.contrib.files import exists
from fabtools.files import upload_template
from fabtools import require
from fabtools.require.files import temporary_directory
from fabtools.utils import run_as_root


# thanks
# http://freepythontips.wordpress.com/2013/07/28/generating-a-random-string/
def _random_generator(size=8, chars=string.ascii_uppercase + string.digits):
    return ''.join(random.choice(chars) for x in range(size))


def _install_packages(package_filter):

    # if we don't want to use a repository but just dpkg --install packages
    if env.manual_package_deploy:
        #it is not optimal, each package will be potentially copied several times, we'll have to improve that
        with temporary_directory() as tmp_dir:
            for package in package_filter:
                if env.debian_packages_path:
                    package = "{path}/{package}".format(path=env.debian_packages_path, package=package)
                put(package, tmp_dir)
            with cd(tmp_dir):
                with warn_only():#@TODO: catch only on error
                    sudo('dpkg --install {}'.format(' '.join(package_filter)))
                #Install dependencies
                sudo('apt-get -f --yes install')
    # else suppose that the machine is configured to use a remote repository
    else:
        # don't want filename package, just the name
        # --quiet to pretty print progress in fabric
        sudo('apt-get --quiet update && apt-get --yes install {}'
        .format(' '.join(package_filter).replace('*deb', '')))


@task
@roles('tyr', 'eng', 'ws')
def stop_puppet():
    run("service puppet stop")


@task
@roles('tyr', 'eng', 'ws')
def start_puppet():
    run("service puppet start")


@task
@roles('tyr_master')
def compute_instance_status(instance):
    """
    check if it is the first deployement of the instance

    the check that we check the existence of the base dir
    """
    print instance.base_ed_dir
    instance.first_deploy = not exists(instance.base_ed_dir)
    print "instance {i} is {s}".format(i=instance.name, s='new' if instance.first_deploy else 'not new')


def require_directory(dirs, is_on_nfs4=False, **kwargs):
    """
    create the directory if it does not exists

    if the directory is on nfs4 (and the environement supports it) we do not chown the directory,
    access right are handled externaly via acl
    """
    print "creating directory {}".format(dirs)
    if env.use_nfs4 and is_on_nfs4:
        print('removing chown')
        if 'owner' in kwargs:
            del kwargs['owner']
        if 'group' in kwargs:
            del kwargs['group']
    require.files.directory(dirs, **kwargs)


def require_directories(dirs, is_on_nfs4=False, **kwargs):
    """
    create the directories if it does not exists

    if the directory is on nfs4 (and the environement supports it) we do not chown the directory,
    access right are handled externaly via acl
    """
    if env.use_nfs4 and is_on_nfs4:
        if 'owner' in kwargs:
            del kwargs['owner']
        if 'group' in kwargs:
            del kwargs['group']
    require.files.directories(dirs, **kwargs)


def _upload_template(filename, destination, context=None, chown=True, user='www-data', **kwargs):
    kwargs['use_jinja'] = True
    kwargs['template_dir'] = os.path.join(os.path.dirname(os.path.realpath(__file__)),
                                          os.path.pardir, 'templates')
    kwargs['context'] = context
    kwargs['mkdir'] = False
    kwargs['chown'] = chown
    kwargs['user'] = user
    kwargs['use_sudo'] = True
    kwargs['backup'] = env.backup_conf_files
    if env.show_diff_when_upload and exists(destination):
        destination_temp = destination + '.temp'
        upload_template(filename, destination_temp, **kwargs)
        with warn_only():
            run('{} {} {}'.format(env.show_diff_when_upload, destination, destination_temp))
        run('rm {}'.format(destination))
        run('mv {} {}'.format(destination_temp, destination))
    else:
        upload_template(filename, destination, **kwargs)


def get_psql_version():
    version_lines = run('psql --version')
    v_line = version_lines.split('\n')[0]
    return v_line.split(" ")[-1].split(".")


def get_host_addr(host):
    """
    get the address of the server from the ssh connection string

    dumb split on the @
    eq root@toto.fr returns toto.fr
    """
    return host.split('@')[-1]


class Parallel:
    """
    run job in multi thread

    syntaxic sugar around multiprocessing.dummy

    use it as RAII eg:
    with Parallel(4) as p:
        p.map(my_function, my_param_array)
    """
    def __init__(self, nb_thread):
        self.pool = ThreadPool(nb_thread)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        print("leaving parallele for")
        self.pool.close()
        self.pool.join()
        print("After join")

    def map(self, func, param):
        self.pool.map(func, param)


def run_once_per_host(func):
    """
    Don't invoke `func` more than once for host and arguments.
    """
    func.past_results = {}
    @functools.wraps(func)
    def decorated(*args, **kwargs):
        key = (env.host_string, args, tuple(kwargs.items()))
        if key not in func.past_results:
            func.past_results[key] = func(*args, **kwargs)
        return func.past_results[key]
    return decorated


def execute_flat(task, *args, **kwargs):
    """ Replacement function for fabric.api.execute() that allows to avoid the squared (or cubed or more)
        execution sequence of decorated tasks.
        For example, if you have these definitions:
        @task
        @hosts('host1', 'host2')
        def task1()
            ...task steps...

        @task
        @hosts('host1', 'host2')
        def task2()
            ...task steps...
            execute(task1)

        @task
        @hosts('host1', 'host2')
        def task3()
            ...task steps...
            execute_flat(task1)

        If you run 'fab task2', you will have the execution sequence (squared):
          task2 on host1
            task1 on host1
            task1 on host2
          task2 on host2
            task1 on host1
            task1 on host2
        If you run 'fab task3', you will have the execution sequence (flattened):
          task3 on host1
            task1 on host1
          task3 on host2
            task1 on host2

        Parameters and return value are identical to execute()
    """
    if env.get('host_string', None):
        return task(*args, **kwargs)
    else:
        return execute(task, *args, **kwargs)


def idempotent_symlink(source, destination, use_sudo=False):
    """
    Create a symbolic link to a file or directory, even if it already exists
    """
    func = use_sudo and run_as_root or run
    func('/bin/ln -sf {} {}'.format(quote(source), quote(destination)))


@contextmanager
def time_that(message):
    """
    measure time of all work done under the 'with'

    print the message with the {elapsed} variable formated with the time
    """
    start_time = time.time()
    yield
    elapsed_time = time.time() - start_time
    print(message.format(elapsed=datetime.timedelta(seconds=elapsed_time)))


# Retrieve Instance from string/unicode if needed
def get_real_instance(instance):
    """ if 'instance' is a string get the corresponding instance, else return the instance """
    if isinstance(instance, basestring):
        return env.instances[instance]
    return instance

host_app_mapping = dict(
    tyr='navitia-tyr',
    eng='navitia-kraken',
    ws='navitia-jormungandr'
)


@task
@run_once_per_host
def apt_get_update():
    sudo('apt-get update')

@task
def restart_apache():
    start_or_stop_with_delay("apache2", 4000, 500, start=False, only_once=True)
    start_or_stop_with_delay("apache2", 4000, 500, only_once=True)

@task
def restart_uwsgi(service):
    """
    Restart uWSGI service
    :param service: uWSGI service to restart.
    """
    start_or_stop_with_delay(service, 4000, 500, start=False, only_once=True)
    start_or_stop_with_delay(service, 4000, 500, only_once=True)


@task
def get_version(host):
    app_name = host_app_mapping[host]
    with settings(host_string=env.roledefs[host][0]):
        execute(apt_get_update)
        lines = run('apt-cache policy %s' % app_name).split('\n')
        try:
            installed = lines[1].strip().split()[-1]
            candidate = lines[2].strip().split()[-1]
        except IndexError:
            installed, candidate = None, None
        return installed, candidate


@task
def show_version(action='show', host='eng'):
    """
    prints, gets or checks versions (installed and candidate) from navitia-kraken/navitia-tyr/navitia-jormungandr package
    show: print versions on stdout
    get: returns tuple (installed, candidate) or (None, None) if navitia-kraken/tyr/jormungandr not installed on target,
         installed and candidate can be tuples if different versions are coexisting
    check: return True if candidate version is different from installed
    """
    versions = execute(get_version, host)
    def summarize(iterable):
        s = tuple(set(iterable))
        if len(s) == 1:
            return s[0]
        return s
    if action == 'show':
        print(green(host_app_mapping[host]))
        for k, v in versions.iteritems():
            print(green("  %s, installed: %s, candidate: %s" % (k, v[0], v[1])))
    elif action == 'get':
        installed = summarize(x[0] for x in versions.itervalues())
        candidate = summarize(x[1] for x in versions.itervalues())
        return installed, candidate
    elif action == 'check':
        if env.manual_package_deploy:
            print(yellow("WARNING Can't check versions of manually installed packages"))
            return True
        installed = summarize(x[0] for x in versions.itervalues())
        candidate = summarize(x[1] for x in versions.itervalues())
        if isinstance(installed, tuple):
            installed = max(installed)
        return semver.compare(candidate, installed) > 0


@task
def update_init(host):
    with settings(host_string=env.roledefs[host][0]):
        with settings(warn_only=True):
            result = run('which systemd')
        if result == '':
            print('systemd is not installed')
        else:
            run('systemctl daemon-reload')

@task
def update_init_kraken(host):
    with settings(host_string=host):
        run('systemctl daemon-reload')

class send_mail(object):
    """
    Mail sending class.
    Class attributes are default messages and subjects
    These can be overriden with keys in env.emails dict.
    env.emails dict must specify at least 'to' and 'server' keys,
    if not, no email is sent and a warning message is printed.
    """
    start_mail_subject = u"Starting deployment of Navitia 2 v{version} on plateform {target}"
    start_mail_message = u"The deployment of the new version of Navitia 2 is starting on plateform %s" \
                         u"\nYou may be impacted."
    finished_mail_subject = u"End of deployment of Navitia 2 v{version} on plateform {target}"
    finished_mail_message = u"New version of Navitia 2 deployed on %s.\n\\o/ thank you for your patience !"
    email_author = (u'Navitia-noreply', u'Navitia Deployment')

    def __init__(self):
        self.mail_load = getattr(env, 'emails', {})
        self.candidate = execute(show_version, action='get').values()[0][1]

    def send_mail(self, message, subject):
        mail = Envelope(
            from_addr=self.mail_load.get('author', self.email_author),
            to_addr=self.mail_load['to'].split(';'),
            subject=subject.format(version=self.candidate, target=env.name.upper()),
            text_body=message % env.name.upper()
        )
        if self.mail_load.get('cc'):
            for cc in self.mail_load['cc'].split(';'):
                mail.add_cc_addr(cc)
        mail.send(self.mail_load['server'])

    def send_start(self, status=None):
        if self.mail_load:
            try:
                end_message = self.mail_load.get('start_mes', self.start_mail_message)
                if status:
                    end_message += status
                self.send_mail(
                    end_message,
                    self.mail_load.get('start_sub', self.start_mail_subject)
                )
            except (KeyError, AttributeError) as e:
                print(yellow("Can't send start email: %r" % e))
        return self

    def send_end(self, status=None):
        if self.mail_load:
            try:
                end_message = self.mail_load.get('end_mes', self.finished_mail_message)
                if status:
                    end_message += status
                self.send_mail(
                    end_message,
                    self.mail_load.get('end_sub', self.finished_mail_subject)
                )
            except (KeyError, AttributeError) as e:
                print(yellow("Can't send finished email: %r" % e))


def get_bool_from_cli(x):
    if isinstance(x, bool):
        return x
    return x.lower() != 'false'


def start_or_stop_with_delay(service, delay, wait, start=True, only_once=False, exc_raise=False):
    cmd = require.service.started if start else require.service.stopped
    retry_cond = (lambda x: not require.service.is_running(service)) if start \
                 else (lambda x: require.service.is_running(service))
    if only_once:
        cmd(service)
        cmd = lambda x: None
        print ('** start_or_stop_with_delay only_once: service={}'.format(service))
    try:
        Retrying(stop_max_delay=delay, wait_fixed=wait,
                 retry_on_result=retry_cond).call(cmd, service)
        print ('** start_or_stop_with_delay retrying: service={}'.format(service))
    except RetryError as e:
        message = "Service {} {} failed: ".format(service, 'start' if start else 'stop') + repr(e)
        if exc_raise:
            raise RuntimeError(message)
        print(red(message))
        print ('** start_or_stop_with_delay error: message={}'.format(message))
        return False
    return True


def show_dead_kraken_status(warn_dict, show=False):
    status = "\n\n---------- Status" \
             "\nThere are {} instances.".format(len(env.instances))

    if warn_dict:
        if warn_dict['jormungandr'] is None and not warn_dict['kraken']:
            status += "\nAll instances are clean and updated!"
        if warn_dict['jormungandr']:
            status += "\nJormungandr version={}".format(warn_dict['jormungandr'])
        if warn_dict['kraken']:
            version_sort = sorted(warn_dict['kraken'], key=lambda i: i.get('kraken_version'), reverse=True)
            for item in version_sort:
                status += "\nKraken {region_id} status={status} version={kraken_version}".format(**item)

    if show:
        print(yellow(status))
    return status


class TimeCollector(object):
    def __init__(self):
        self.start_time = {}
        self.end_time = {}

    def register_start(self, service):
        self.start_time[service] = datetime.datetime.now()

    def register_end(self, service):
        self.end_time[service] = datetime.datetime.now()

    def get_duration(self, service, format=None):
        du = self.end_time[service] - self.start_time[service]
        if format == 'minutes':
            return du.total_seconds()/60
        elif format == 'hours':
            return du.total_seconds()/3600
        else:
            return du


def show_time_deploy(td, show=False):
    time_deploy = ''
    if 'total_deploy' in td.end_time:
        time_deploy += "\nTotal deployment time: {} ({:.2f} hours)".\
            format(td.get_duration('total_deploy'), td.get_duration('total_deploy', format='hours'))
    if 'bina' in td.end_time:
        time_deploy += "\nBinarization time: {} ({:.2f} hours)".\
            format(td.get_duration('bina'), td.get_duration('bina', format='hours'))
    if 'kraken' in td.end_time:
        time_deploy += "\nKraken reload time: {} ({:.2f} minutes)".\
            format(td.get_duration('kraken'), td.get_duration('kraken', format='minutes'))
    if time_deploy:
        time_deploy = "\n\n--------- Time" + time_deploy

    if show and time_deploy:
        print(yellow(time_deploy))

    return time_deploy


class SupervisionHandler(object):
    __metaclass__ = ABCMeta

    @abstractmethod
    def stop_supervision(self, host, service, duration=None):
        """
        stop the supervision for a service of a host
        Optional: definition of a timer
        """
        return

    @abstractmethod
    def start_supervision(self, host, service):
        """
        start or restart the supervision for a service of a host
        """
        return


class NagiosSupervisionHandler(SupervisionHandler):
    def __init__(self, url, user, pwd):
        self.url = url
        self.user = user
        self.pwd = pwd

    def format_host(self, host):
        """the nagio supervisor only wants a short host name"""
        move_host = re.compile(r'@(.*?)\.')
        return move_host.search(host).group(1)

    def stop_supervision(self, host, service, duration=None):
        if not duration:
            raise ValueError("'duration' attribute must be defined and positive.")

        start_dt = datetime.datetime.now()
        start_str = start_dt.strftime('%d-%m-%Y %H:%M:%S')

        end_dt = start_dt + datetime.timedelta(minutes=duration)
        end_str = end_dt.strftime('%d-%m-%Y %H:%M:%S')

        ctp_host = self.format_host(host)
        message = 'Deployment'

        datas = {'cmd_typ': 56, 'cmd_mod': 2, 'host': ctp_host, 'service': service,
                 'com_author': self.user, 'com_data': message, 'trigger': 0,
                 'start_time': start_str, 'end_time': end_str, 'fixed': 1,
                 'hours': 2, 'minutes': 0, 'btnSubmit': 'Commit'}

        requests.get(self.url, auth=HTTPBasicAuth(self.user, self.pwd), params=datas, verify=False)

    def start_supervision(self, host, service):
        """
        for the moment we don't know how to put back the supervision
        TODO
        """
        pass


@task
def login_nagios(self):
    run("curl --cookie-jar /tmp/thruk.cookie -H 'Cookie: thruk_theme=Thruk; thruk_backends={}={}={}={}; "
        "thruk_conf=; thruk_auth=; thruk_message=; thruk_test=%2A%2A%2A%2A; _ga={}' '{}' "
        "--data 'referer=&login={}&password={}&submit=login'"
        .format(self.thruk_backends[0], self.thruk_backends[1], self.thruk_backends[2], self.thruk_backends[3],
                self.ga, self.url + 'cgi-bin/login.cgi', self.user, self.pwd))
    with hide('output'), settings(warn_only=True):
        parse_response = run("curl --cookie /tmp/thruk.cookie '{}'".format(self.url))
    for line in parse_response.split('\n'):
        if line.startswith('var user_token'):
            token = line.split('=')[1].strip()
            return token[1:-2]
    return None


@task
def stop_supervision(self, message, data, start_str, end_str, ctp_host, service, token):
            run("curl --cookie /tmp/thruk.cookie '{}'  --data 'referer={}&token={}&cmd_typ={}&cmd_mod={}&host={}"
                "&service={}&com_data={}&trigger={}&start_time={}&end_time={}&fixed={}&hours={}&minutes={}&backend={}"
                "&backend.orig={}&btnSubmit={}'"
                .format(self.url + 'cgi-bin/cmd.cgi', message, token, data['cmd_typ'], data['cmd_mod'], ctp_host, service,
                        message, data['trigger'], start_str, end_str, data['fixed'], data['hours'], data['minutes'],
                        self.backend, self.backend, data['btnSubmit']))


class ThrukSupervisionHandler(SupervisionHandler):

    def __init__(self, thruk_backends, ga, url, user, pwd, backend, host_support):
        self.thruk_backends = thruk_backends
        self.ga = ga
        self.url = url
        self.user = user
        self.pwd = pwd
        self.backend = backend
        self.host_support = host_support

    def format_host(self, host):
        """the Thruk supervisor only wants a short host name"""
        move_host = re.compile(r'@(.*?)\.')
        return move_host.search(host).group(1)

    def authentification(self):
        """
        TODO
        Use Requests library
        """
        return login_nagios(self)

    def stop_supervision(self, host, service, duration=None):
        if not duration:
            raise ValueError("'duration' attribute must be defined and positive.")

        data = {'cmd_typ': 56, 'cmd_mod': 2, 'trigger': 0, 'fixed': 1, 'hours': 2, 'minutes': 0, 'btnSubmit': 'Commit'}
        start_dt = datetime.datetime.now()
        start_str = start_dt.strftime('%Y-%m-%d+%H:%M:%S')

        end_dt = start_dt + datetime.timedelta(minutes=duration)
        end_str = end_dt.strftime('%Y-%m-%d+%H:%M:%S')

        ctp_host = self.format_host(host)
        message = 'Deployment'

        with settings(host_string=env.default_ssh_user + '@' + self.host_support):
            # need to keep login in cookie before to deactivation the supervision
            token = env.supervision_handler.authentification()
            stop_supervision(self, message, data, start_str, end_str, ctp_host, service, token)

    def start_supervision(self, host, service):
        """
        for the moment we don't know how to put back the supervision
        TODO
        """
        pass


@task
def supervision_downtime(step):
    """
    during the deployment, the supervision is stopping for specific services
    """
    if not env.supervision_handler:
        return

    step_data = env.supervision_config[step]
    if step_data['downtime']:
        for host in step_data['hosts']:
            if '{instance}' in step_data['service']:
                for instance in env.instances:
                    service = step_data['service'].format(instance=instance)
                    env.supervision_handler.stop_supervision(host, service, step_data['downtime'])
            else:
                env.supervision_handler.stop_supervision(host, step_data['service'], step_data['downtime'])


def collapse_op(hosts, op=dict):
    """ Decorator to automatically execute a function on a specific host or multiple hosts,
        collecting return values in a dict or a list, or just testing them.
        The decorated function must have its first argument a host specifier (string).
        Usage: if the wrapper function is called with host='host' parameter, the wrapped
        function will be called once with this host.
        If the wrapper function is called with no host= parameter, the wrapped function will
        be called once per host of the platform. The wrapped function will then return a
        value depending of the op parameter.
        :param op:
        - dict:  a dictionary {host: value, ...} of the return values for each host.
        - extend or append: a concatenated list of return values, use 'extend' if
          the wrapped function returns a sequence, otherwise use 'append'.
        - and: a boolean, execution on multiple hosts stops as soon as the wrapped
          function returns False (good to check path or process exists on all hosts).
        - any: a boolean, execution on multiple hosts stops as soon as the wrapped
          function returns True (good to check missing path or process on all hosts).
    """
    op_values = ('dict', 'extend', 'append', 'and', 'any')
    def wrapper(meth):
        def wrapped(*args, **kwargs):
            host = kwargs.pop('host', None)
            if host:
                return meth(host, *args, **kwargs)
            elif op == 'dict':
                return {host: meth(host, *args, **kwargs) for host in hosts}
            elif op in ('extend', 'append'):
                ret = []
                for host in hosts:
                    getattr(ret, op)(meth(host, *args, **kwargs))
                return ret
            elif op == 'and':
                return all(meth(host, *args, **kwargs) for host in hosts)
            elif op == 'any':
                return any(meth(host, *args, **kwargs) for host in hosts)
            else:
                raise ValueError("Parameter op must be one of {}".format(op_values))
        return wrapped
    return wrapper


def run_command_on_host(host, cmd):
    with settings(host_string=host):
        stdout = StringIO()
        run(cmd, stdout=stdout)
        return stdout.getvalue().strip()


def get_processes(host, cmd="ps -A", filter=None):
    if filter:
        if isinstance(filter, basestring):
            cmd += " | grep '{}'".format(filter)
        else:
            cmd += " | grep -E '{}'".format('|'.join(filter))
    with settings(warn_only=True):
        ret = []
        for line in run_command_on_host(host, cmd).split('\n'):
            line = line.strip()
            if line:
                line = line.split()
                if len(line) > 2:
                    ret.append(line[-1])
        return ret


@contextmanager
def watchdog_manager(target, *args, **kwargs):
    th = Thread(target=target, *args, **kwargs)
    th.start()
    yield th
    th.join()
