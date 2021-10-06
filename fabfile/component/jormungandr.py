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


import json
import re
import requests
from requests.auth import HTTPBasicAuth
from requests.exceptions import ConnectionError
from simplejson.scanner import JSONDecodeError
from urllib2 import HTTPError

from fabric.colors import red, green, blue, yellow
from fabric.context_managers import settings
from fabric.decorators import roles
from fabric.operations import run
from fabric.api import execute, task, env, sudo
from fabtools import require, python

# WARNING: the way fabric_navitia imports are done as a strong influence
#          on the resulting naming of tasks, wich can break integration tests
from fabfile.component import load_balancer
from fabfile.utils import (_install_packages, _upload_template, get_real_instance,
                           start_or_stop_with_delay, get_bool_from_cli, show_version,
                           restart_apache, restart_uwsgi)


@task
@roles('ws')
def update_jormungandr_conf():
    """
    update the jormungandr configuration
    """
    require.files.directories([env.jormungandr_base_dir, env.jormungandr_instances_dir, env.jormungandr_log_dir],
                              owner=env.KRAKEN_USER, group=env.KRAKEN_USER, use_sudo=True)

    _upload_template('jormungandr/jormungandr.wsgi.jinja', env.jormungandr_wsgi_file,
                     context={
                         'env': env
                     })
    _upload_template('jormungandr/settings.py.jinja', env.jormungandr_settings_file,
                     context={'env': env})
    if env.newrelic_key: # Only upload newrelic config if a license key is provided
        _upload_template('jormungandr/newrelic.ini.jinja', env.jormungandr_newrelic_config_file,
                         context={'env': env})

    if env.uwsgi_enable:
        # Add uwsgi config for jormungandr
        _upload_template('jormungandr/jormungandr.ini.jinja', env.jormungandr_uwsgi_config_file,
                         context={'env': env}, backup=False)


@task
@roles('ws')
def setup_jormungandr():
    require.users.user('www-data')

    execute(update_jormungandr_conf)

    if env.setup_apache:
        sudo('sudo a2enmod rewrite')
        _upload_template('jormungandr/jormungandr_apache_config.jinja', env.jormungandr_apache_config_file,
                         context={'env': env}, backup=False)

    execute(start_jormungandr_all)


@task
@roles('ws')
# @runs_once
def upgrade_ws_packages():
    packages = [
        'apache2',
        'libapache2-mod-wsgi',
        'logrotate',
        'redis-server',
        'python2.7',
        'git',
        'gcc',
        'python-dev',
        'protobuf-compiler'
    ]
    if env.distrib in ('ubuntu14.04', 'debian8'):
        packages.append('libzmq3-dev')
        packages.append('libgeos-3.4.2')
    elif env.distrib == 'debian7':
        packages.append('libzmq-dev')

    require.deb.packages(packages)
    package_filter_list = ['navitia-jormungandr*deb',
                           'navitia-common*deb']
    _install_packages(package_filter_list)
    if not python.is_pip_installed():
        python.install_pip()

    # We want the version of the system for these packages
    run('''sed -e "/protobuf/d" -e "/psycopg2/d"  /usr/share/jormungandr/requirements.txt > /tmp/jormungandr_requirements.txt''')
    #add newrelic as a dependency
    run('''echo 'newrelic==2.70.0.51' >> /tmp/jormungandr_requirements.txt''')
    run('git config --global url."https://".insteadOf git://')
    require.python.install_requirements('/tmp/jormungandr_requirements.txt', use_sudo=True, exists_action='w')


@task
def reload_jormun_safe(server, safe=True):
    """ Reload jormungandr on a specific server,
        in a safe way if load balancers are available
    """

    safe = get_bool_from_cli(safe)

    with settings(host_string=server):
        if env.use_load_balancer and safe:
            load_balancer.disable_node(server)

        # Restart uWSGI if set.
        if env.uwsgi_enable:
            restart_uwsgi('jormungandr')

        else:
            restart_apache()

        if env.use_load_balancer and safe:
            load_balancer.enable_node(server)


@task
def reload_jormun_safe_all(safe=True, reverse=False):
    """ Reload jormungandr on all servers,
        in a safe way if load balancers are available
    """
    safe = get_bool_from_cli(safe)
    for server in (env.roledefs['ws'][::-1] if reverse else env.roledefs['ws']):
        execute(reload_jormun_safe, server, safe)


@task
def start_jormungandr_all():
    """ Start jormungadr on all servers """
    start_services()
    for server in env.roledefs['ws']:
        execute(reload_jormun_safe, server, False)


@task
@roles('ws')
def start_services():
    start_or_stop_with_delay('apache2', env.APACHE_START_DELAY * 1000, 500, only_once=env.APACHE_START_ONLY_ONCE)


@task
def check_kraken_jormun_after_deploy(show=False):

    headers = {'Host': env.jormungandr_url}
    request_str = 'http://{}{}/v1/status'.format(env.jormungandr_url, env.jormungandr_url_prefix)

    print("request_str: {}".format(request_str))

    try:
        # Send HTTP GET requests
        response = requests.get(request_str, headers=headers, auth=HTTPBasicAuth(env.token, ''))

        # If HTTP status_code Erreur.
        if response.status_code != 200:
            print(red("Request not successful : {}".format(str(response))))
            return None

        result = response.json()

    except (ConnectionError, HTTPError) as e:
        print(red("HTTP Error {}: {}".format(e.code, e.readlines()[0])))
        return None
    except JSONDecodeError as e:
        print(red("cannot read json response : {}".format(e)))
        return None
    except Exception as e:
        print(red("Error when connecting to {}: {}".format(env.jormungandr_url, e)))
        return None

    warn_dict = {'jormungandr': None, 'kraken': []}

    if re.match(r"v{}".format(show_version(action='get')[1]), result['jormungandr_version']):
        warn_dict['jormungandr'] = result['jormungandr_version']

    for item in result['regions']:

        kraken_warn = {'status': item['status'], 'region_id': item['region_id']}

        if item['status'] == "dead":
            kraken_warn['kraken_version'] = None
        elif item['kraken_version'] != warn_dict['jormungandr']:
            kraken_warn['kraken_version'] = item['kraken_version']
        elif item['status'] == "no_data":
            kraken_warn['kraken_version'] = warn_dict['jormungandr']

        if 'kraken_version' in kraken_warn.keys():
            warn_dict['kraken'].append(kraken_warn)

    if show:
        if warn_dict['jormungandr']:
            print(yellow("Jormungandr version={}".format(warn_dict['jormungandr'])))
        for item in warn_dict['kraken']:
            print(yellow("status={status} | region_id={region_id} | kraken_version={kraken_version}".format(**item)))

    return warn_dict


@task
def test_jormungandr(server, instance=None, fail_if_error=True):
    """
    Test jormungandr globally (/v1/coverage) or a given instance

    Note: we don't launch that with a role because we want to test the access from the outside of the server
    """
    headers = {'Host': env.jormungandr_url}

    request_str = 'http://{}{}/v1/coverage'.format(server, env.jormungandr_url_prefix)

    technical_requests = {
        'vehicle_journeys': 'http://{}{}/v1/coverage/{}/vehicle_journeys?count=1'.format(server,
                                                                                         env.jormungandr_url_prefix,
                                                                                         instance),
        'stop_points': 'http://{}{}/v1/coverage/{}/stop_points?count=1'.format(server, env.jormungandr_url_prefix,
                                                                               instance)
    }

    result = {}

    if instance:
        request_str = 'http://{}{}/v1/coverage/{}/status'.format(server, env.jormungandr_url_prefix, instance)

    try:
        response = requests.get(request_str, headers=headers, auth=HTTPBasicAuth(env.token, ''))

        response.raise_for_status()
        print("{} -> {}".format(response.url, green(response.status_code)))

        if instance:
            for query_type, url in technical_requests.items():
                r = requests.get(url, headers=headers, auth=HTTPBasicAuth(env.token, ''))

                # Raise error if status_code != 200
                if r.status_code != 200 or query_type not in r.json().keys() or 'error' in r.json().keys():
                    print("{} ({}) -> {}".format(query_type, yellow(r.status_code), r.json()['error']))
                else:
                    print("{} -> {}".format(query_type, green(r.status_code)))

        result = response.json()

    except (ConnectionError, HTTPError) as e:
        if fail_if_error:
            print(red("Connection or HTTP Error {}".format(e)))
            exit(1)
        else:
            print(yellow("WARNING: {} is running but problem found: {} (maybe no data ?)".format(instance, e)))
            exit(0)
    except JSONDecodeError as e:
        print(red("cannot read json response : {}".format(e)))
        exit(1)
    except Exception as e:
        print(red("Error when connecting to %s: %s" % (env.jormungandr_url, e)))
        exit(1)

    # if result contain just a message, this indicate a problem
    if 'message' in result and fail_if_error:
        print(red("CRITICAL: Problem on result: '{}'".format(result)))
        exit(1)
    elif 'message' in result:
        print(yellow("WARNING: Problem on result: '{}'".format(result)))
        return False

    if instance:
        print(green("Kraken Version is {}".format(result['status']['kraken_version'])))
    # just check that there is one instance running
    else:
        regions = result['regions']

        active_instance = [i for i in env.instances.keys() if i not in env.excluded_instances]

        if len(regions) != len(active_instance):
            print red("there is not the right number of instances, "
                      "we should have {ref} but we got {real} instances".
                      format(ref=len(active_instance), real=len(regions)))

            print red('instances in diff: {}'.format(
                set(active_instance).symmetric_difference(set([i['id'] for i in regions]))))

            if fail_if_error:
                exit(1)

            return False
        else:
            # We check that at least one is ok
            statuses = [(region['id'], region['status']) for region in regions]

            if all(map(lambda p: p[1] == 'running', statuses)):
                print green('all instances are ok, everything is fine')
                return True

            print blue('running instances: {}'.format(
                [status[0] for status in statuses if status[1] == 'running']
            ))
            print red('KO instances: {}'.format([status for status in statuses if status[1] != 'running']))

            if fail_if_error:
                exit(1)
            return False

    return True


@task
@roles('ws')
def deploy_jormungandr_instance_conf(instance):
    """ Deploy or redeploy one jormungander coverage:
        * Deploy the json configuration file
        * Do not reload apache
    """
    instance = get_real_instance(instance)
    config = {'key': instance.name,
              'zmq_socket': instance.jormungandr_zmq_socket_for_instance,
              'realtime_proxies': instance.realtime_proxies}
    if instance.pt_zmq_socket:
        config['pt_zmq_socket'] = instance.pt_zmq_socket
    if instance.street_network:
        config["street_network"] = instance.street_network
    if instance.ridesharing:
        config["ridesharing"] = instance.ridesharing
    if instance.autocomplete:
        config["default_autocomplete"] = instance.autocomplete
    if instance.equipment_details_providers:
        config["equipment_details_providers"] = instance.equipment_details_providers
    _upload_template("jormungandr/instance.json.jinja",
                     instance.jormungandr_config_file,
                     context={
                         'json': json.dumps(config, indent=4)
                     },
                     use_sudo=True
    )


@task
@roles('ws')
def remove_jormungandr_instance(instance):
    """ Remove a jormungandr instance entirely
        * Remove json file which declare the instance
        * Reload apache
    """
    instance = get_real_instance(instance)
    run("rm --force {}" .format(instance.jormungandr_config_file))


@task
def deploy_jormungandr_all_instances_conf():
    """ Deploy all jormungander coverages:
        * Deploy all json configuration files
        * Reload apache
    """
    for instance in env.instances.values():
        execute(deploy_jormungandr_instance_conf, instance)

    reload_jormun_safe_all()
