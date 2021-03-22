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

from fabric.api import env, task, execute, abort
from fabric.colors import blue, red, yellow, green
from fabric.context_managers import settings
from fabric.contrib.files import exists
from component import tyr, jormungandr, kraken, db, haproxy
from component.load_balancer import _adc_connection
from utils import (get_bool_from_cli, show_version, get_host_addr,
                   show_dead_kraken_status, TimeCollector, compute_instance_status,
                   show_time_deploy, host_app_mapping, send_mail,
                   supervision_downtime, get_real_instance)
from prod_tasks import (remove_kraken_vip, switch_to_first_phase, enable_kraken_haproxy,
                        switch_to_second_phase, switch_to_third_phase, enable_nodes)
import random
import requests
import component


#############################################
#                                           #
#           SETUP                           #
#                                           #
#############################################


@task
def deploy_from_scratch(kraken_wait='serial'):
    """
    deploy navitia on empty server
    This task can also be called on a already configured environment, it should only update all
    :param kraken_wait: 'serial' or 'parallel'
    """
    env.show_diff_when_upload = ''
    execute(setup)
    execute(update_all_instances, kraken_wait=kraken_wait)
    execute(upgrade_all)


@task
def setup():
    """
    setup the environement.
    install all requirements, deploy the needed configuration
    """
    execute(upgrade_all_packages)
    execute(component.db.setup_db)
    execute(tyr.setup_tyr_master)
    execute(tyr.setup_tyr)
    execute(kraken.setup_kraken)
    execute(jormungandr.setup_jormungandr)
    execute(tyr.upgrade_db_tyr)


#############################################
#                                           #
#           UPGRADE (COMMON)                #
#                                           #
#############################################

@task
def upgrade_all_packages():
    """ Upgrade all navitia packages """
    execute(tyr.upgrade_tyr_packages)
    execute(tyr.setup_tyr_master)
    execute(kraken.upgrade_engine_packages)
    execute(kraken.upgrade_monitor_kraken_packages)
    execute(tyr.upgrade_ed_packages)
    execute(jormungandr.upgrade_ws_packages)


@task
def upgrade_all(up_tyr=True, up_confs=True, upgrade_db_tyr=True, check_version=True, send_mail='no',
                manual_lb=False, check_dead=True, check_bina=True, skip_bina=False):
    """Upgrade all navitia packages, databases and launch rebinarisation of all instances """
    up_tyr = get_bool_from_cli(up_tyr)
    up_confs = get_bool_from_cli(up_confs)
    check_version = get_bool_from_cli(check_version)
    check_dead = get_bool_from_cli(check_dead)
    check_bina = get_bool_from_cli(check_bina)
    upgrade_db_tyr = get_bool_from_cli(upgrade_db_tyr)
    skip_bina = get_bool_from_cli(skip_bina)

    # check if all krakens are running with data
    not_loaded_instances = kraken.get_not_loaded_instances_per_host()

    # check one instance on each WS
    #TODO: Check all instance not only random one. #pylint: disable=fixme
    for server in env.roledefs['ws']:
        instance = random.choice(env.instances.values())
        execute(jormungandr.test_jormungandr, get_host_addr(server), instance=instance.name)

    if check_version:
        execute(compare_version_candidate_installed, host_name='tyr')

    if env.use_load_balancer:
        if manual_lb:
            print(yellow("WARNING : you are in MANUAL mode :\n"
                         "Check frequently for message asking you to switch nodes manually"))
        else:
            # check credential NOW
            _adc_connection(check=True)

    execute(check_last_dataset)
    if send_mail in ('start', 'all'):
        broadcast_email('start')

    time_dict = TimeCollector()
    time_dict.register_start('total_deploy')

    if up_tyr:
        execute(update_tyr_step, time_dict, only_bina=False, check_bina=check_bina, upgrade_db_tyr=upgrade_db_tyr, skip_bina=skip_bina)

    if check_version:
        execute(compare_version_candidate_installed)

    if not skip_bina:
        execute(kraken.swap_all_data_nav)

    # Upgrade kraken/jormun on first hosts set
    if env.eng_hosts_1 and env.ws_hosts_1:
        env.roledefs['eng'] = env.eng_hosts_1
        env.roledefs['ws'] = env.ws_hosts_1
        if manual_lb:
            raw_input(yellow("Please disable ENG1,3/WS7-9 and enable ENG2,4/WS10-12"))
        else:
            execute(switch_to_first_phase, env.eng_haproxy1, env.ws_hosts_1, env.ws_hosts_2)
    execute(upgrade_kraken, wait=env.KRAKEN_RESTART_SCHEME, up_confs=up_confs, supervision=True)
    if check_dead:
        execute(kraken.check_dead_instances, not_loaded_instances)
    execute(upgrade_jormungandr, reload=False, up_confs=up_confs)
    # need restart apache without using upgrade_jormungandr task previously
    # because that causes a problem in prod
    execute(jormungandr.reload_jormun_safe_all, safe=False)

    # check one instance on each WS
    #TODO: Check all instance not only random one.
    for server in env.roledefs['ws']:
        instance = random.choice(env.instances.values())
        execute(jormungandr.test_jormungandr, get_host_addr(server), instance=instance.name)

    if env.eng_hosts_2 and env.ws_hosts_2:
        # Upgrade kraken/jormun on remaining hosts
        env.roledefs['eng'] = env.eng_hosts_2
        env.roledefs['ws'] = env.ws_hosts_2
        if manual_lb:
            raw_input(yellow("Please enable ENG1,3/WS7-9 and disable ENG2,4/WS10-12"))
        else:
            execute(switch_to_second_phase, env.eng_haproxy1, env.eng_haproxy2,
                    env.ws_hosts_1,  env.ws_hosts_2)
        execute(upgrade_jormungandr, reload=False, up_confs=up_confs)
        # need restart apache without using upgrade_jormungandr task previously
        # because that causes a problem in prod
        execute(jormungandr.reload_jormun_safe_all, safe=False)
        if manual_lb:
            raw_input(yellow("Please enable WS7-12"))
        else:
            execute(switch_to_third_phase, env.ws_hosts_2)
        env.roledefs['ws'] = env.ws_hosts
        execute(upgrade_kraken, wait=env.KRAKEN_RESTART_SCHEME, up_confs=up_confs)
        if check_dead:
            execute(kraken.check_dead_instances, not_loaded_instances)
        # check second hosts set
        for server in env.roledefs['ws']:
            instance = random.choice(env.instances.values())
            execute(jormungandr.test_jormungandr, get_host_addr(server), instance=instance.name)

        env.roledefs['eng'] = env.eng_hosts
        if not manual_lb:
            #execute(enable_nodes, env.eng_hosts)
            execute(enable_kraken_haproxy, env.eng_names)

    # start tyr_beat even if up_tyr is False
    execute(tyr.start_tyr_beat)
    time_dict.register_end('total_deploy')
    if send_mail in ('end', 'all'):
        warn_dict = jormungandr.check_kraken_jormun_after_deploy()
        status = show_dead_kraken_status(warn_dict, show=True)
        status += show_time_deploy(time_dict)
        broadcast_email('end', status)

    if env.use_load_balancer and manual_lb:
        print(yellow("Please enable ENG1-4/WS7-12"))


@task
def broadcast_email(kind, status=None):
    if not hasattr(env, 'mail_class'):
        env.mail_class = send_mail()
    if kind == 'start':
        env.mail_class.send_start(status)
    elif kind == 'end':
        env.mail_class.send_end(status)


@task

def update_tyr_step(time_dict=None, only_bina=True, up_confs=True, check_bina=False, upgrade_db_tyr=True, skip_bina=False):
    # TODO only_bina is highly error prone
    """ deploy an upgrade of tyr
    """
    if not time_dict:
        time_dict = TimeCollector()
    execute(tyr.stop_tyr_beat)
    execute(upgrade_tyr, up_confs=up_confs, pilot_tyr_beat=False, upgrade_db_tyr=upgrade_db_tyr )
    time_dict.register_start('bina')
    if skip_bina:
        return time_dict
    instances_failed = execute(tyr.launch_rebinarization_upgrade, pilot_tyr_beat=False).values()[0]
    if check_bina and instances_failed:
        if float(len(instances_failed)) / len(env.instances) <= env.acceptable_bina_fail_rate:
            print(yellow("  WARNING: {} binarisation(s) have failed, process again".format(len(instances_failed))))
            instances_failed = execute(tyr.launch_rebinarization_upgrade,
                                       pilot_supervision=False,
                                       pilot_tyr_beat=False,
                                       instances=instances_failed).values()[0]
        else:
            print(yellow("  WARNING: Too many ({}) binarisations have failed, do not process again".
                         format(len(instances_failed))))
    time_dict.register_end('bina')
    if only_bina:
        print show_time_deploy(time_dict)
        return None
    if check_bina and instances_failed:
        abort(red("\n  ERROR: {} binarisation(s) have failed.".format(len(instances_failed))))
    return time_dict


@task
def compare_version_candidate_installed(host_name='eng'):
    """Check candidate version is different from installed"""
    if not show_version(action='check', host=host_name):
        installed_version, candidate_version = show_version(action='get', host=host_name)
        message = "Candidate {} version ({}) is older or the same than " \
                  "the installed one ({})."\
            .format(host_app_mapping[host_name], candidate_version, installed_version)
        abort(message)


@task
def upgrade_tyr(up_confs=False, pilot_tyr_beat=True, reload=True, upgrade_db_tyr=True):
    """Upgrade all ed instances db, launch bina"""
    if pilot_tyr_beat:
        execute(tyr.stop_tyr_beat)
    execute(tyr.upgrade_tyr_packages)
    execute(tyr.setup_tyr_master)
    execute(tyr.upgrade_ed_packages)
    execute(tyr.upgrade_db_tyr, pilot_tyr_beat=pilot_tyr_beat, upgrade_db_tyr=upgrade_db_tyr)
    if up_confs:
        tyr.update_tyr_confs()
    execute(tyr.upgrade_cities_db)
    if reload:
        execute(tyr.reload_tyr_safe_all)
    if pilot_tyr_beat:
        restart_tyr(pilot_tyr_beat)


@task
def restart_tyr(tyr_beat=True):
    # restart tyr workers and reload with newer binaries
    execute(tyr.restart_tyr_worker)
    if tyr_beat:
        execute(tyr.restart_tyr_beat)


@task
def restart_kraken():
    print ('** test_deploy_multi_threads restart_all_krakens with wait = {}'.format(wait))
    execute(kraken.restart_all_krakens)


@task
def restart_jormungandr():
    """ This task is now SAFE on PROD
    """
    execute(jormungandr.reload_jormun_safe_all)


@task
def restart_all():
    execute(component.db.start_services)
    execute(tyr.start_services)
    execute(jormungandr.start_services)
    restart_tyr()
    restart_kraken()
    restart_jormungandr()


@task
def upgrade_version():
    """
    install a new version and update databases.
    Does not launch data binarization
    It is used mainly for artemis where we don't want to bother launching the binarization
    """
    # upgrade packages anywhere
    execute(upgrade_all_packages)
    execute(upgrade_tyr, reload=False)
    for instance in env.instances.values():
        execute(tyr.update_ed_db, instance.name)


@task
def upgrade_kraken(wait='serial', up_confs=True, supervision=False):
    """Upgrade and restart all kraken instances"""
    print ('** test_deploy_multi_threads upgrade_kraken with wait = {}'.format(wait))
    if supervision:
        supervision_downtime(step='kraken')
    execute(kraken.upgrade_engine_packages)
    execute(kraken.upgrade_monitor_kraken_packages)
    for instance in env.instances.values():
        execute(kraken.set_kraken_binary, instance)
    if up_confs:
        execute(kraken.update_monitor_configuration)
        for instance in env.instances.values():
            execute(kraken.update_eng_instance_conf, instance)
    execute(kraken.restart_all_krakens, wait=wait)


@task
def upgrade_jormungandr(reload=True, up_confs=True):
    """Upgrade and restart all jormun instances"""
    execute(jormungandr.upgrade_ws_packages)
    if up_confs:
        execute(jormungandr.update_jormungandr_conf)
        for instance in env.instances.values():
            execute(jormungandr.deploy_jormungandr_instance_conf, instance)
    if reload:
        execute(jormungandr.reload_jormun_safe_all)


def dataset_exists(filename):
    with settings(host_string=env.roledefs['tyr_master'][0]):
        return exists(filename)


@task
def check_last_dataset():
    """Check the data before upgrade"""
    datasets = {'ok': [], 'ko': [], 'empty': []}
    nb_ko = 0

    for instance in env.instances.values():
        url = 'http://{}/v0/instances/{}/last_datasets'.format(env.tyr_url, instance)
        try:
            status = requests.get(url)
        except Exception as e:
            abort("Request failed: {} ({})".format(url, e))

        if status.json():
            for elt in status.json():
                filename = elt['name']
                family_type = elt['family_type']
                # check existence of data file and remove into bdd if file is missing
                if not dataset_exists(filename):
                    datasets['ko'].append({'instance': instance.name, 'filename': filename, 'type': family_type})
                    nb_ko += 1
                else:
                    datasets['ok'].append({'instance': instance.name, 'filename': filename, 'type': family_type})
        else:
            datasets['empty'].append(instance.name)

    if len(datasets['ok']):
        print("******** AVAILABLE DATASETS ********")
        for data in datasets['ok']:
            print(green(data['filename']))
    if len(datasets['ko']):
        print("********* MISSING DATASETS *********")
        for data in datasets['ko']:
            print(red(data['filename']))
    if len(datasets['empty']):
        print("********** EMPTY DATASETS **********")
        for data in datasets['empty']:
            print(yellow(data))

    if nb_ko > 0:
        exit(1)


#############################################
#                                           #
#           CRUD (COMMON)                   #
#                                           #
#############################################

@task
def update_all_instances(kraken_wait='serial'):
    print ('** test_deploy_multi_threads update_all_instances with wait = {}'.format(kraken_wait))
    """
    update all the instances
    if the instance does not exists, deploy it
    TODO: we could detect the deleted instances to remove them
    """
    print(blue('creating all instances'))
    for instance in env.instances.values():
        execute(update_instance, instance)
    execute(kraken.restart_all_krakens, wait=kraken_wait)


@task
def update_all_configurations(restart=True):
    print ('** test_deploy_multi_threads update_all_configurations called')
    """
    update all configuration and restart all services
    does not deploy any packages
    """
    restart = get_bool_from_cli(restart)
    
    execute(kraken.get_no_data_instances)
    execute(jormungandr.update_jormungandr_conf)
    execute(kraken.update_monitor_configuration)
    execute(tyr.update_tyr_confs)
    for instance in env.instances.values():
        execute(tyr.update_tyr_instance_conf, instance)
        execute(jormungandr.deploy_jormungandr_instance_conf, instance)
        execute(kraken.update_eng_instance_conf, instance)
    #once all has been updated, we restart all services (if needed)for the conf to be taken into account
    if restart:
        execute(tyr.restart_tyr_worker)
        execute(tyr.restart_tyr_beat)
        execute(jormungandr.reload_jormun_safe_all)
        execute(kraken.restart_all_krakens)

        # and we test the jormungandr
        for server in env.roledefs['ws']:
            jormungandr.test_jormungandr(get_host_addr(server))

@task
def update_jormungandr_configurations(restart=True):
    """
    update all configuration and restart all services
    does not deploy any packages
    """
    restart = get_bool_from_cli(restart)
    
    execute(jormungandr.update_jormungandr_conf)
    for instance in env.instances.values():
        execute(jormungandr.deploy_jormungandr_instance_conf, instance)
    #once all has been updated, we restart all services (if needed)for the conf to be taken into account
    if restart:
        execute(jormungandr.reload_jormun_safe_all)
        
        # and we test the jormungandr
        for server in env.roledefs['ws']:
            jormungandr.test_jormungandr(get_host_addr(server))

@task
def update_instance(instance, reload_jormun=True):
    """
    param (instance) - update all configuration and restart all services
    does not deploy any packages
    """
    instance = get_real_instance(instance)
    reload_jormun = get_bool_from_cli(reload_jormun)
    print(blue('updating {}'.format(instance.name)))
    #first of all we compute the instance status, it will be helpfull later
    execute(compute_instance_status, instance)
    execute(tyr.create_tyr_instance, instance)
    execute(component.db.postgis_initdb, instance.db_name)
    execute(tyr.update_ed_db, instance.name)
    execute(jormungandr.deploy_jormungandr_instance_conf, instance)
    execute(kraken.create_eng_instance, instance)
    execute(tyr.deploy_default_synonyms, instance)
    execute(component.db.create_privileges_instance_db, instance)
    if reload_jormun:
        execute(jormungandr.reload_jormun_safe_all)


@task
def remove_instance(instance, admin=False):
    """Completely remove all components for a given instance,
    Remove instance in jormungandr db
    """
    instance = get_real_instance(instance)
    execute(component.db.remove_instance_from_jormun_database, instance)
    execute(component.db.remove_postgresql_database, instance.db_name)
    execute(component.db.remove_postgresql_user, instance.db_user)
    execute(tyr.remove_ed_instance, instance)
    execute(tyr.remove_tyr_instance, instance)
    execute(kraken.remove_kraken_instance, instance)
    execute(jormungandr.remove_jormungandr_instance, instance)
    if admin and env.use_load_balancer:
        execute(remove_kraken_vip, instance)
    execute(jormungandr.reload_jormun_safe_all)


@task
def clean_instances(clean=False):
    """ Show and clean tyr instances still in DB but removed from conf
    """
    tyr_instances = [instance['name'] for instance in requests.get("http://{}/v0/instances".format(env.tyr_url)).json()]
    print("instances count, jormun: {}, conf: {}".format(len(tyr_instances), len(env.instances)))
    instances_to_clean = set(tyr_instances).difference(env.instances)
    print("Instances in jormun DB not in conf: {}".format(instances_to_clean))
    print("Instances in conf not in jormun DB: {}".format(set(env.instances).difference(tyr_instances)))
    if instances_to_clean:
        if clean:
            print("Removing instances: {}".format(instances_to_clean))
            for instance in instances_to_clean:
                execute(component.db.remove_instance_from_jormun_database, instance)
            print("Done.")
        else:
            print("You can specify parameter 'clean=True' to clean jormun DB.")
