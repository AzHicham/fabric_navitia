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

import os.path
from retrying import Retrying
import simplejson as json
import requests
from collections import namedtuple

from fabric.api import task, env, sudo, execute
from fabric.colors import blue, red, green, yellow
from fabric.context_managers import settings
from fabric.contrib.files import exists, is_link
from fabric.decorators import roles
from fabric.operations import run
from fabric.utils import abort
from fabtools import require, service, files, python

# WARNING: the way fabric_navitia imports are done as a strong influence
#          on the resulting naming of tasks, wich can break integration tests
from fabfile.utils import (get_bool_from_cli, _install_packages, get_real_instance,
                           update_init, update_init_kraken, get_host_addr,
                           _upload_template, start_or_stop_with_delay, idempotent_symlink)


@task
@roles('eng')
def setup_kraken():
    require.users.user('www-data')
    require.files.directories([env.kraken_basedir, env.kraken_log_basedir,
        env.kraken_monitor_basedir], owner=env.KRAKEN_USER, group=env.KRAKEN_USER,
        use_sudo=True)
    update_monitor_configuration()
    if env.setup_apache:
        apache_conf_path = env.apache_conf_path('monitor-kraken')
        _upload_template('kraken/monitor_apache_config.jinja', apache_conf_path,
                     context={'env': env}, backup=False)
        if env.apache_version() >= 2.4:
            sudo('a2enconf monitor-kraken.conf')
            sudo("service apache2 reload")
    require.service.started('apache2')


@task
@roles('eng')
def upgrade_engine_packages():
    packages = ['logrotate', 'python2.7', 'gcc', 'python-dev', 'apache2', 'libapache2-mod-wsgi']
    if env.distrib in ('ubuntu14.04', 'debian8'):
        packages.append('libzmq3-dev')
    elif env.distrib == 'debian7':
        packages.append('libzmq-dev')
    require.deb.packages(packages, update=True)
    # copy the old binary before installing the new one
    if exists('/usr/bin/kraken'):
        run("cp /usr/bin/kraken /usr/bin/kraken.old")
    package_filter_list = ['navitia-kraken*deb',
                           'navitia-kraken-dbg*deb']
    _install_packages(package_filter_list)


@task
def rollback_instance(instance, test=True):
    """ Use this only if something goes wrong during deployment of an instance
    """
    test = get_bool_from_cli(test)
    instance = get_real_instance(instance)
    execute(swap_data_nav, instance, force=True)
    execute(set_kraken_binary, instance, old=True)
    execute(restart_kraken, instance, wait=env.KRAKEN_RESTART_SCHEME if test else 'no_test')


@task
def set_kraken_binary(instance, old=False):
    instance = get_real_instance(instance)
    for host in instance.kraken_engines:
        with settings(host_string=host):
            kraken_bin = "{}/{}/kraken".format(env.kraken_basedir, instance.name)
            idempotent_symlink('/usr/bin/kraken' + ('.old' if old else ''), kraken_bin, use_sudo=True)


@task
@roles('eng')
def upgrade_monitor_kraken_packages():
    package_filter_list = ['navitia-monitor-kraken*deb']
    _install_packages(package_filter_list)
    if not python.is_pip_installed():
        python.install_pip()
    require.python.install_requirements('/usr/share/monitor_kraken/requirements.txt',
                                        use_sudo=True,
                                        exists_action='w')


@task
def get_no_data_instances():
    """ Get instances that have no data loaded ("status": null)"""
    for instance in env.instances.values():
        for host in instance.kraken_engines:
            instance_has_data = test_kraken(instance, fail_if_error=False, hosts=[host])
            if not instance_has_data:
                target_file = instance.kraken_database
                with settings(host_string=host):
                    target_file_exists = exists(target_file)
                if not target_file_exists:
                    env.excluded_instances.append(instance.name)
                    print(blue("NOTICE: no data for {}, append it to exclude list"
                               .format(instance.name)))
                else:
                    print(red("CRITICAL: instance {} is not available but *has* a "
                        "{}, please inspect manually".format(instance.name, target_file)))
                break


@task
@roles('eng')
def require_monitor_kraken_started():
    start_or_stop_with_delay('apache2', env.APACHE_START_DELAY * 1000, 500, only_once=env.APACHE_START_ONLY_ONCE)


@task
def restart_all_krakens(wait='serial'):
    """restart and test all kraken instances"""
    execute(require_monitor_kraken_started)
    instances = tuple(env.instances)
    for index, instance in enumerate(env.instances.values()):
        restart_kraken(instance, wait=wait)
        left = instances[index + 1:]
        if left:
            print(blue("Instances left: {}".format(','.join(left))))


@task
def require_all_krakens_started():
    """start each kraken instance if it is not already started"""
    execute(require_monitor_kraken_started)
    for instance in env.instances.values():
        require_kraken_started(instance)


@task
def test_all_krakens(wait=False):
    """test all kraken instances"""
    wait = get_bool_from_cli(wait)
    for instance in env.instances.values():
        test_kraken(instance, fail_if_error=False, wait=wait, loaded_is_ok=True)


@task
@roles('tyr_master')
def swap_all_data_nav(force=False):
    for instance in env.instances.values():
        swap_data_nav(instance, force)


@task
@roles('tyr_master')
def swap_data_nav(instance, force=False):
    """ swap old/new data.nav.lz4, only if new is still in temp directory
    """
    plain_target = get_real_instance(instance).target_lz4_file
    temp_target = os.path.join(os.path.dirname(plain_target), 'temp', os.path.basename(plain_target))
    if exists(plain_target):
        if exists(temp_target) and \
           (force or (files.getmtime(temp_target) > files.getmtime(plain_target))):
            swap_temp = os.path.join(os.path.dirname(temp_target), 'x')
            files.move(plain_target, swap_temp)
            files.move(temp_target, plain_target)
            files.move(swap_temp, temp_target)
    elif exists(temp_target):
        files.move(temp_target, plain_target)


@task
@roles('tyr_master')
def purge_data_nav(force=False):
    """
    purge temp/data.nav.lz4 files
    the whole process will be skipped as soon as a single condition is encountered:
    - temp data file is more recent than actual data file
    - temp data file exists but actual data file is missing
    """
    if not force:
        print("Checking lz4 temp files purge conditions before proceeding...")
        reason = {}
        for instance in env.instances.values():
            plain_target = get_real_instance(instance).target_lz4_file
            temp_target = os.path.join(os.path.dirname(plain_target), 'temp', os.path.basename(plain_target))
            if exists(plain_target):
                if exists(temp_target) and files.getmtime(temp_target) > files.getmtime(plain_target):
                    reason[instance.name] = "{} is more recent than {}".format(temp_target, plain_target)
            elif exists(temp_target):
                reason[instance.name] = "{} does not exists".format(plain_target)
        if reason:
            print(yellow("Error: Can't purge lz4 temp files, reasons:"))
            for k, v in reason.iteritems():
                print("  {}: {}".format(k, v))
            exit(1)

    for instance in env.instances.values():
        plain_target = get_real_instance(instance).target_lz4_file
        temp_target = os.path.join(os.path.dirname(plain_target), 'temp', os.path.basename(plain_target))
        if exists(temp_target):
            files.remove(temp_target)


@task
def get_not_loaded_instances_per_host():
    instance_host = namedtuple('InstanceAndHost', ['instance', 'host'])
    not_loaded = []
    for instance in env.instances.values():
        for host in instance.kraken_engines_url:
            request = 'http://{}:{}/{}/?instance={}'.format(host, env.kraken_monitor_port,
                                                            env.kraken_monitor_location_dir, instance.name)
            result = _test_kraken(request, fail_if_error=False)
            if not result or 'error' in result or result['status'] == 'timeout' or result['loaded'] is False:
                not_loaded.append(instance_host(instance=instance.name, host=host))

    if not_loaded:
        print("Not loaded instances:")
        for coverage in not_loaded:
            print(yellow("Instance {} to {}".format(coverage.instance, coverage.host)))
    return not_loaded


@task
def check_dead_instances(not_loaded_instances):
    new_not_loaded = get_not_loaded_instances_per_host()  # we fetch again the not loaded instances

    difference = set(new_not_loaded) - set(not_loaded_instances)

    if difference:
        print("Dead instances because of deployment:")
        for coverage in difference:
            print(red("Instance {} to {}".format(coverage.instance, coverage.host)))
        exit(1)


@task
def restart_kraken(instance, wait='serial'):
    """ Restart all krakens of an instance (using pool), serially or in parallel,
        then test them. Testing serially assures that krakens are restarted serially.
        :param wait: string.
               Possible values=False or None: restart in parallel, no test
               'serial': restart serially and test
               'parallel': restart in parallel and test
               'no_test': explicitely skip tests (faster but dangerous)
        The default value is 'serial' because it is the safest scenario
        to restart the krakens of an instance in production.
    """
    if wait not in ('serial', 'parallel', 'no_test'):
        abort(yellow("Error: wait parameter must be 'serial', 'parallel' or 'no_test', found '{}'".format(wait)))
    instance = get_real_instance(instance)
    excluded = instance.name in env.excluded_instances
    # restart krakens of this instance that are also in the eng role,
    # this works with the "pool" switch mechanism used in upgrade_all()
    for host in set(instance.kraken_engines).intersection(env.roledefs['eng']):
        restart_kraken_on_host(instance, host)
        if wait == 'serial' and not excluded:
            test_kraken(instance, fail_if_error=False, wait=True, hosts=[host])
    if wait == 'parallel' and not excluded:
        test_kraken(instance, fail_if_error=False, wait=True)
    if wait != 'no_test' and excluded:
        print(yellow("Coverage '{}' has no data, not testing it".format(instance.name)))
    if wait == 'no_test':
        print(yellow("Warning Coverage '{}' not tested: parameter wait='no_test'".format(instance.name)))


@task
def restart_kraken_on_host(instance, host):
    """ Restart a kraken of an instance on a given server
    """
    instance = get_real_instance(instance)
    with settings(host_string=host):
        kraken = 'kraken_' + instance.name
        start_or_stop_with_delay(kraken, 4000, 500, start=False, only_once=True)
        start_or_stop_with_delay(kraken, 4000, 500, only_once=env.KRAKEN_START_ONLY_ONCE)


@task
def require_kraken_started(instance):
    """start a kraken instance on all servers if it is not already started
    """
    instance = get_real_instance(instance)
    kraken = 'kraken_' + instance.name
    for host in instance.kraken_engines:
        with settings(host_string=host):
            start_or_stop_with_delay(kraken, 4000, 500, only_once=True)


@task
def stop_kraken(instance):
    """Stop a kraken instance on all servers
    """
    instance = get_real_instance(instance)
    kraken = 'kraken_' + instance.name
    for host in instance.kraken_engines:
        with settings(host_string=host):
            start_or_stop_with_delay(kraken, 4000, 500, start=False, only_once=True)


def _test_kraken(query, fail_if_error=True):
    """
    poll on kraken monitor until it gets a 'running' status
    """
    print("calling : {}".format(query))
    try:
        response = requests.get(query, timeout=5)
    except requests.exceptions.Timeout as t:
        print("timeout error {}".format(t))
        if fail_if_error:
            exit(1)
        else:
            return None
    except Exception as e:
        print("Error when connecting to monitor: %s" % e)
        exit(1)

    return json.loads(response.text)


@task
def test_kraken(instance, fail_if_error=True, wait=False, loaded_is_ok=None, hosts=None):
    """Test kraken with '?instance='"""
    instance = get_real_instance(instance)
    wait = get_bool_from_cli(wait)

    hosts = [h.split('@')[1] for h in hosts or instance.kraken_engines]
    will_return = len(hosts) == 1
    for host in hosts:
        request = 'http://{}:{}/{}/?instance={}'.format(host,
            env.kraken_monitor_port, env.kraken_monitor_location_dir, instance.name)

        if wait:
            # we wait until we get a response and the instance is 'loaded'
            try:
                result = Retrying(stop_max_delay=env.KRAKEN_RESTART_DELAY * 1000,
                                  wait_fixed=1000, retry_on_result=lambda x: x is None or not x['loaded']) \
                    .call(_test_kraken, request, fail_if_error)
            except Exception as ex:
                print(red("ERROR: could not reach {}, too many retries ! ({})".format(instance.name, ex)))
                result = {'status': False}
        else:
            result = _test_kraken(request, fail_if_error)

        try:
            if result['status'] != 'running':
                if result['status'] == 'no_data':
                    print(yellow("WARNING: instance {} has no loaded data".format(instance.name)))
                    if will_return:
                        return False
                if fail_if_error:
                    print(red("ERROR: Instance {} is not running ! ({})".format(instance.name, result)))
                    if will_return:
                        return False
                print(yellow("WARNING: Instance {} is not running ! ({})".format(instance.name, result)))
                if will_return:
                    return False

            if not result['is_connected_to_rabbitmq']:
                print(yellow("WARNING: Instance {} is not connected to rabbitmq".format(instance.name)))
                if will_return:
                    return False

            if loaded_is_ok is None:
                loaded_is_ok = wait
            if not loaded_is_ok:
                if result['loaded']:
                    print(yellow("WARNING: instance {} has loaded data".format(instance.name)))
                    if will_return:
                        return True
                else:
                    print(green("OK: instance {} has correct values: {}".format(instance.name, result)))
                    if will_return:
                        return False
            else:
                if result['loaded']:
                    print(green("OK: instance {} has correct values: {}".format(instance.name, result)))
                    if will_return:
                        return True
                elif fail_if_error:
                    abort(red("CRITICAL: instance {} has no loaded data".format(instance.name)))
                else:
                    print(yellow("WARNING: instance {} has no loaded data".format(instance.name)))
                    if will_return:
                        return False
        except KeyError:
            print(red("CRITICAL: instance {} does not return a correct result".format(instance.name)))
            print(result)
            if fail_if_error:
                abort('')
        return False


@task
@roles('eng')
def update_monitor_configuration():

    _upload_template('kraken/monitor_kraken.wsgi.jinja', env.kraken_monitor_wsgi_file,
            context={'env': env})
    _upload_template('kraken/monitor_settings.py.jinja', env.kraken_monitor_config_file,
            context={'env': env})


@task
def update_eng_instance_conf(instance, host=None):
    instance = get_real_instance(instance)
    hosts = [host] if host else instance.kraken_engines
    for h in hosts:
        with settings(host_string=h):
            require.files.directory(os.path.join(instance.kraken_basedir, instance.name),
                                    owner=env.KRAKEN_USER, group=env.KRAKEN_USER, use_sudo=True)
            _upload_template("kraken/kraken.ini.jinja", "%s/%s/kraken.ini" %
                             (env.kraken_basedir, instance.name),
                             context={
                                 'env': env,
                                 'instance': instance,
                             }
            )

            if env.use_systemd:
                _upload_template("kraken/systemd_kraken.jinja",
                                 "{}".format(env.service_name('kraken_{}'.format(instance.name))),
                                 context={'env': env,
                                          'instance': instance.name,
                                          'kraken_base_conf': env.kraken_basedir,
                                 },
                                 mode='644'
                )
            else:
                _upload_template("kraken/kraken.initscript.jinja",
                                 "{}".format(env.service_name('kraken_{}'.format(instance.name))),
                                 context={'env': env,
                                          'instance': instance.name,
                                          'kraken_base_conf': env.kraken_basedir,
                                 },
                                 mode='755'
                )
            update_init_kraken(host=h)


@task
def create_eng_instance(instance):
    """ Create a new kraken instance (idempotent)
        * Install requirements
        * Deploy the binary, the templatized ini configuration in a dedicated
          directory with rights to www-data and the logdir
        * Deploy initscript and add it to startup
        * Start the service
    """
    instance = get_real_instance(instance)
    for host in instance.kraken_engines:
        with settings(host_string=host):
            # base_conf
            require.files.directory(instance.kraken_basedir,
                                    owner=env.KRAKEN_USER, group=env.KRAKEN_USER, use_sudo=True)
            # logs
            require.files.directory(env.kraken_log_basedir,
                                    owner=env.KRAKEN_USER, group=env.KRAKEN_USER, use_sudo=True)

            update_eng_instance_conf(instance, host)

            # kraken.ini, pid and binary symlink
            kraken_bin = "{}/{}/kraken".format(env.kraken_basedir, instance.name)
            if not is_link(kraken_bin):
                idempotent_symlink("/usr/bin/kraken", kraken_bin, use_sudo=True)
                sudo('chown -h {user} {bin}'.format(user=env.KRAKEN_USER, bin=kraken_bin))

            kraken = "kraken_{}".format(instance.name)
            if not service.is_running(kraken):
                if env.use_systemd:
                    sudo("systemctl enable kraken_{}.service".format(instance.name))
                else:
                    sudo("update-rc.d kraken_{} defaults".format(instance.name))
                print(blue("INFO: kraken {instance} instance is starting on {server}, "
                           "waiting 5 seconds, we will check if processus is running"
                    .format(instance=instance.name, server=get_host_addr(env.host_string))))
                service.start(kraken)
                run("sleep 5")  # we wait a bit for the kraken to pop

            with settings(warn_only=True):
                run("pgrep --list-name --full /srv/kraken/{}/kraken".format(instance.name))
            print(blue("INFO: kraken {instance} instance is running on {server}".
                       format(instance=instance.name, server=get_host_addr(host))))


@task
def remove_kraken_instance(instance, purge_logs=False, apply_on='engines'):
    """
    Remove a kraken instance entirely
      * Stop the service
      * Remove startup at boot time
      * Remove initscript
      * Remove configuration and pid directory
    apply_on values:
     - engines: apply on instance.kraken_engines
     - reverse: apply on all engines except instance.kraken_engines
     - all: apply on all engines
    """
    instance = get_real_instance(instance)
    if apply_on == 'engines':
        hosts, exclude_hosts = instance.kraken_engines, ()
    elif apply_on == 'reverse':
        hosts, exclude_hosts = env.roledefs['eng'], instance.kraken_engines
    elif apply_on == 'all':
        hosts, exclude_hosts = env.roledefs['eng'], ()
    else:
        abort("Bad 'apply_on' parameter value: {}".format(apply_on))

    for host in set(hosts) - set(exclude_hosts):
        with settings(
            host_string=host,
            warn_only=True
        ):
            print("INFO: removing kraken instance {} from {}".format(instance.name, get_host_addr(host)))

            service.stop('kraken_{}'.format(instance.name))
            run("sleep 3")
            if env.use_systemd:
                run("systemctl disable kraken_{}.service".format(instance.name))
                run('systemctl daemon-reload')
                run("rm -f {}/kraken_{}.service".format(env.service_path(), instance.name))
            else:
                run("rm -f {}/kraken_{}".format(env.service_path(), instance.name))
            run("rm -rf {}/{}/".format(env.kraken_basedir, instance.name))
            if purge_logs:
                run("rm -f {}/{}.log".format(env.kraken_log_basedir, instance.name))


@task
def delete_kraken_queue_to_rabbitmq(instance, apply_on='reverse'):
    """
    Remove queue for a kraken
    """
    instance = get_real_instance(instance)
    if apply_on == 'engines':
        hosts, exclude_hosts = instance.kraken_engines, ()
    elif apply_on == 'reverse':
        hosts, exclude_hosts = env.roledefs['eng'], instance.kraken_engines
    elif apply_on == 'all':
        hosts, exclude_hosts = env.roledefs['eng'], ()
    else:
        abort("Bad 'apply_on' parameter value: {}".format(apply_on))

    host_string = env.rabbitmq_kraken_host

    for host in set(hosts) - set(exclude_hosts):
        with settings(host_string=host_string):
            run('curl -i -u {}:{} -XDELETE "http://localhost:{}/api/queues/{}/kraken_{}_{}_rt"'
                .format(env.rabbitmq_kraken_user, env.rabbitmq_kraken_pass, env.rabbitmq_port_api,
                       rabbitmq_kraken_vhost, get_host_addr(host).split('.')[0], instance))
            run('curl -i -u {}:{} -XDELETE "http://localhost:{}/api/queues/{}/kraken_{}_{}_task"'
                .format(env.rabbitmq_kraken_user, env.rabbitmq_kraken_pass, env.rabbitmq_port_api,
                        rabbitmq_kraken_vhost, get_host_addr(host).split('.')[0], instance))


@task
def delete_all_kraken_queues_to_rabbitmq():
    """
    Remove all queues
    """
    for instance in env.instances.values():
        execute(delete_kraken_queue_to_rabbitmq, instance.name)


@task
def is_not_synchronized(instance, hosts=None):
    """
    test if an instance has unsynchronized kraken
    to test that we consider 'publication_date' of the kraken's data

    for the moment it's a manually called function
    """
    instance = get_real_instance(instance)
    hosts = [e.split('@')[1] for e in hosts or instance.kraken_engines]
    publication_dates = set()
    for host in hosts:
        request = 'http://{}:{}/{}/?instance={}'.format(host,
            env.kraken_monitor_port, env.kraken_monitor_location_dir, instance.name)
        result = _test_kraken(request, fail_if_error=False)
        publication_dates.add(result.get('publication_date'))

    print('publication date for {}, : {}'.format(instance, publication_dates))
    if len(publication_dates) != 1:
        print(red('all the {} krakens do not have the same data loaded, publication dates: {}'.format(
            instance, publication_dates)))
        return 1
    return 0


@task
def check_kraken_data_synchronization():
    """
    check if there is some instance with not synchronized data
    (eg: one engine has different data from the others)

    for the moment it's a manually called function
    """
    res = 0
    for instance in env.instances:
        res += is_not_synchronized(instance)

    if res:
        print(red('{} krakens have not synchronized data'.format(res)))
    else:
        print(green('All krakens have synchronized data'))


@task
def redeploy_kraken(instance, create=True):
    """
    Redistributes an existing kraken on eng engines.
    Call this task when the zmq_server parameter of add_instance is changed.
    Use create=False if krakens mapping is reduced (this avoids restarting them).
    Use create=True if krakens are displaced or mapping is expanded.
    """
    instance = get_real_instance(instance)
    create = get_bool_from_cli(create)
    if create:
        execute(create_eng_instance, instance)
    execute(remove_kraken_instance, instance, purge_logs=True, apply_on='reverse')


@task
def redeploy_all_krakens(create=True):
    """
    Redistributes all krakens on eng engines according to zmq_server parameters
    """
    create = get_bool_from_cli(create)
    for instance in env.instances.values():
        redeploy_kraken(instance, create)
