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

import datetime
from datetime import datetime
import os

from fabric.api import run, env, task, execute, roles, abort
from fabric.colors import blue, red, yellow, green
from fabric.contrib.files import exists

from fabfile.component import tyr, db, jormungandr, kraken
from fabfile.component.kraken import check_dead_instances
from fabfile.component.load_balancer import get_adc_credentials, _adc_connection
from fabfile import utils
from fabfile.utils import (get_bool_from_cli, show_version,
                           show_dead_kraken_status, TimeCollector, show_time_deploy)
from prod_tasks import (remove_kraken_vip, switch_to_first_phase,
                        switch_to_second_phase, enable_all_nodes)


#############################################
#                                           #
#           SETUP                           #
#                                           #
#############################################


@task
def deploy_from_scratch():
    """
    deploy navitia on empty server
    This task can also be called on a already configured environment, it should only update all
    """
    #execute(retrieve_packages) # this is not working and i don't think it is the right way to do this
    execute(setup)
    execute(update_all_instances, kraken_wait=False)
    execute(upgrade_all, kraken_wait=False)

@task
def setup():
    """
    setup the environement.
    install all requirements, deploy the needed configuration
    """
    execute(upgrade_all_packages)
    execute(db.setup_db)
    execute(tyr.setup_tyr)
    execute(tyr.setup_tyr_master)
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
def upgrade_all(up_tyr=True, up_confs=True, kraken_wait=True, check_version=True,
                send_mail='no', check_dead=True, manual_lb=True):
    """Upgrade all navitia packages, databases and launch rebinarisation of all instances """
    check_version = get_bool_from_cli(check_version)
    if check_version:
        execute(compare_version_candidate_installed)
    up_tyr = get_bool_from_cli(up_tyr)
    up_confs = get_bool_from_cli(up_confs)
    check_dead = get_bool_from_cli(check_dead)
    kraken_wait = get_bool_from_cli(kraken_wait)
    manual_lb = get_bool_from_cli(manual_lb)
    if env.use_load_balancer:
        if manual_lb:
            print(yellow("WARNING : you are in MANUAL mode :\n"
                         "Check frequently for message asking you to switch nodes manually"))
        else:
            get_adc_credentials()
            # check credential NOW
            _adc_connection(check=True)
    execute(check_last_dataset)
    if send_mail in ('start', 'all'):
        broadcast_email('start')

    time_dict = TimeCollector()
    time_dict.register_start('total_deploy')
    execute(tyr.stop_tyr_beat)
    if up_tyr:
        execute(upgrade_tyr, up_confs=up_confs, pilot_tyr_beat=False)
        time_dict.register_start('bina')
        execute(tyr.launch_rebinarization_upgrade, pilot_tyr_beat=False)
        time_dict.register_end('bina')
        execute(kraken.swap_all_data_nav)

    if env.use_load_balancer:
        # Upgrade kraken/jormun on first hosts set
        env.roledefs['eng'] = env.eng_hosts_1
        env.roledefs['ws'] = env.ws_hosts_1
        time_dict.register_start('kraken')
        if manual_lb:
            raw_input(yellow("Please disable ENG1/WS1 and enable ENG2-4/WS2-4"))
        else:
            execute(switch_to_first_phase, env.eng_hosts_1, env.ws_hosts_1, env.ws_hosts_2)
        execute(upgrade_kraken, kraken_wait=kraken_wait, up_confs=up_confs)
        if check_dead:
            execute(check_dead_instances)
        execute(upgrade_jormungandr, reload=False, up_confs=up_confs)

        # Upgrade kraken/jormun on remaining hosts
        env.roledefs['eng'] = env.eng_hosts_2
        env.roledefs['ws'] = env.ws_hosts_2
        if manual_lb:
            raw_input(yellow("Please enable ENG1/WS1 and disable ENG2-4/WS2-4"))
        else:
            execute(switch_to_second_phase, env.eng_hosts_1, env.eng_hosts_2,
                    env.ws_hosts_1,  env.ws_hosts_2)
        execute(upgrade_kraken, kraken_wait=kraken_wait, up_confs=up_confs)
        time_dict.register_end('kraken')
        execute(upgrade_jormungandr, reload=False, up_confs=up_confs)
        if not manual_lb:
            execute(enable_all_nodes, env.eng_hosts, env.ws_hosts_1,  env.ws_hosts_2)
        env.roledefs['eng'] = env.eng_hosts
        env.roledefs['ws'] = env.ws_hosts
    else:
        time_dict.register_start('kraken')
        execute(upgrade_kraken, kraken_wait=kraken_wait, up_confs=up_confs)
        time_dict.register_end('kraken')
        execute(upgrade_jormungandr, up_confs=up_confs)

    execute(tyr.start_tyr_beat)
    time_dict.register_end('total_deploy')
    warn_dict = jormungandr.check_kraken_jormun_after_deploy()
    status = show_dead_kraken_status(warn_dict, show=True)
    status += show_time_deploy(time_dict)
    if send_mail in ('end', 'all'):
        broadcast_email('end', status)
    if env.use_load_balancer and manual_lb:
        print(yellow("Please enable ENG1-4/WS1-4"))


@task
def deploy_prod_bina(up_confs=True, check_version=True, send_mail=False):
    """Upgrade all navitia packages, databases and launch rebinarisation of all instances """
    check_version = get_bool_from_cli(check_version)
    up_confs = get_bool_from_cli(up_confs)
    if check_version:
        execute(compare_version_candidate_installed('navitia-tyr'))
    execute(check_last_dataset)
    if send_mail:
        broadcast_email('start')

    time_dict = TimeDiff()
    time_dict.register_start('total_deploy')
    execute(tyr.stop_tyr_beat)

    execute(upgrade_tyr, up_confs=up_confs, pilot_tyr_beat=False)
    time_dict.register_start('bina')
    execute(tyr.launch_rebinarization_upgrade, pilot_tyr_beat=False)
    time_dict.register_end('bina')

    print(yellow("beginning = {} || bina = {:.2f}"
                 .format(time_dict.get_time_diff('total_deploy'),
                         time_dict.get_time_diff('bina', format='hours'))))

@task
def deploy_prod_update(up_confs=True, kraken_wait=True, send_mail=False,
                       check_dead=True, check_version=True, time_bina='no'):
    up_confs = get_bool_from_cli(up_confs)
    check_dead = get_bool_from_cli(check_dead)
    kraken_wait = get_bool_from_cli(kraken_wait)
    execute(kraken.swap_all_data_nav)
    if check_version:
        execute(compare_version_candidate_installed)
    execute(check_last_dataset)

    time_dict = TimeDiff()

    # Upgrade kraken/jormun on first hosts set
    env.roledefs['eng'] = env.eng_hosts_1
    env.roledefs['ws'] = env.ws_hosts_1
    time_dict.register_start('kraken')
    raw_input(yellow("Please disable ENG1/WS1 and enable ENG2-4/WS2-4"))
    execute(upgrade_kraken, kraken_wait=kraken_wait, up_confs=up_confs)
    if check_dead:
        execute(check_dead_instances)
    execute(upgrade_jormungandr, reload=False, up_confs=up_confs)

    # Upgrade kraken/jormun on remaining hosts
    env.roledefs['eng'] = env.eng_hosts_2
    env.roledefs['ws'] = env.ws_hosts_2
    raw_input(yellow("Please enable ENG1/WS1 and disable ENG2-4/WS2-4"))
    execute(upgrade_kraken, kraken_wait=kraken_wait, up_confs=up_confs)
    time_dict.register_end('kraken')
    execute(upgrade_jormungandr, reload=False, up_confs=up_confs)
    env.roledefs['eng'] = env.eng_hosts
    env.roledefs['ws'] = env.ws_hosts

    execute(tyr.start_tyr_beat)
    time_end = datetime.now()
    warn_dict = jormungandr.check_kraken_jormun_after_deploy()
    status = show_dead_kraken_status(warn_dict, show=True)
    status += show_time_deploy(time_dict)
    status += "\nBinarization time: {} hours".format(time_bina)
    status += "\n\nEnd deployment : {}\n".format(time_end)
    if send_mail:
        broadcast_email('end', status)
    print(yellow("Please enable ENG1-4/WS1-4"))


@task
def broadcast_email(kind, status=None):
    if not hasattr(env, 'mail_class'):
        env.mail_class = utils.send_mail()
    if kind == 'start':
        env.mail_class.send_start()
    elif kind == 'end':
        env.mail_class.send_end(status)


@task
def compare_version_candidate_installed(app_name='navitia-kraken'):
    """Check candidate version is different from installed"""
    if not show_version(action='check'):
        if app_name == 'navitia-kraken':
            installed_version, candidate_version = show_version(action='get')
            message = "Candidate kraken version ({}) is older or the same than the installed one ({}).".format(candidate_version, installed_version)
        elif app_name == 'navitia-tyr':
            installed_version, candidate_version = show_version(action='get', app_name='navitia-tyr')
            message = "Candidate tyr version ({}) is older or the same than the installed one ({}).".format(candidate_version, installed_version)
        abort(message)

@task
def upgrade_tyr(up_confs=False, pilot_tyr_beat=True):
    """Upgrade all ed instances db, launch bina"""
    if pilot_tyr_beat:
        execute(tyr.stop_tyr_beat)
    execute(tyr.upgrade_tyr_packages)
    execute(tyr.setup_tyr_master)
    execute(tyr.upgrade_ed_packages)
    execute(tyr.upgrade_db_tyr)
    if up_confs:
        tyr.update_tyr_confs()
    restart_tyr(pilot_tyr_beat)

@task
def restart_tyr(tyr_beat=True):
    # restart tyr workers and reload with newer binaries
    execute(tyr.restart_tyr_worker)
    if tyr_beat:
        execute(tyr.restart_tyr_beat)

@task
def restart_kraken():
    execute(kraken.restart_all_krakens)

@task
def restart_jormungandr():
    """ This task is now SAFE on PROD
    """
    execute(jormungandr.reload_jormun_safe_all)

@task
def restart_all():
    execute(db.start_services)
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
    execute(upgrade_tyr)
    for instance in env.instances.values():
        execute(tyr.update_ed_db, instance.name)

@task
def upgrade_kraken(kraken_wait=True, up_confs=True):
    """Upgrade and restart all kraken instances"""
    kraken_wait = get_bool_from_cli(kraken_wait)
    execute(kraken.upgrade_engine_packages)
    execute(kraken.upgrade_monitor_kraken_packages)
    if up_confs:
        execute(kraken.update_monitor_configuration)
        for instance in env.instances.values():
            execute(kraken.update_eng_instance_conf, instance)
    execute(kraken.restart_all_krakens, wait=kraken_wait)

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

@task
@roles("tyr_master")
def isset_dataset(filename=None):
    return exists(filename)

@task
@roles("db")
def check_last_dataset():
    """Check the data before upgrade"""
    datasets_pending = {}
    datasets = {'ok': [], 'ko': [], 'pending': [], 'empty': []}
    nb_ko = 0

    date_stopchecking = datetime.datetime.now() - datetime.timedelta(days=10)
    str_date = date_stopchecking.strftime("%Y-%m-%d")

    for instance in env.instances.values():
        datasets_pending[instance.name] = []
        res = run('sudo -i -u postgres psql -A -t -c '
              '"select distinct on (data_set.family_type) data_set.name, data_set.family_type '
              '  from instance, job, data_set '
              '  where instance.id = job.instance_id and job.id = data_set.job_id and instance.name=\'%s\' and job.state=\'done\' '
              '  order by data_set.family_type desc, job.created_at desc;" jormungandr' % instance.name)
        arr_dataset = res.split('\n')
        for dataset in arr_dataset:
            if dataset != "":
                fil, typ = dataset.split("|")
                filname = os.path.split(fil)[1]
                isset = execute(isset_dataset, fil)
                if not isset:
                    datasets['ko'].append({'instance': instance.name, 'file': fil, 'type': typ, 'filename': filname})
                    nb_ko += 1
                else:
                    datasets['ok'].append({'instance': instance.name, 'file': fil, 'type': typ, 'filename': filname})
            else:
                datasets['empty'].append(instance.name)

        res = run('sudo -i -u postgres psql -A -t -c '
              '"select distinct on (data_set.family_type) data_set.name, data_set.family_type, job.created_at '
              '  from instance, job, data_set '
              '  where instance.id = job.instance_id and job.id = data_set.job_id and instance.name=\'{}\' and job.state=\'pending\' and job.created_at > \'{}\' '
              '  order by data_set.family_type desc, job.created_at desc;" jormungandr'.format(instance.name, str_date))
        arr_dataset = res.split('\n')
        for dataset in arr_dataset:
            if dataset != "":
                (fil, typ, dat) = dataset.split("|")
                filname = os.path.split(fil)[1]
                datasets['pending'].append({'instance': instance.name, 'file': fil, 'type': typ,
                                            'filename': filname, 'date': dat})
                datasets_pending[instance.name].append(filname)

    if len(datasets['ok']):
        print("******** AVAILABLE DATASETS ********")
        for data in datasets['ok']:
            print(green(data['file']))
    if len(datasets['ko']):
        print("********* MISSING DATASETS *********")
        for data in datasets['ko']:
            if data['filename'] in datasets_pending[data['instance']]:
                print(red(data['file']) + yellow(" (Pending)"))
            else:
                print(red(data['file']))
    if len(datasets['pending']):
        print("********* PENDING DATASETS *********")
        for data in datasets['pending']:
            print(yellow(data['file'] + " since " + data['date']))
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
def update_all_instances(kraken_wait=True):
    """
    update all the instances
    if the instance does not exists, deploy it
    TODO: we could detect the deleted instances to remove them
    """
    kraken_wait = get_bool_from_cli(kraken_wait)
    print(blue('creating all instances'))
    for instance in env.instances.values():
        execute(update_instance, instance)
    execute(kraken.restart_all_krakens, wait=kraken_wait)

@task
def update_all_configurations():
    """
    update all configuration and restart all services
    does not deploy any packages
    """
    execute(kraken.get_no_data_instances)
    execute(jormungandr.update_jormungandr_conf)
    execute(kraken.update_monitor_configuration)
    execute(tyr.update_tyr_conf)
    for instance in env.instances.values():
        execute(tyr.update_tyr_instance_conf, instance)
        execute(jormungandr.deploy_jormungandr_instance_conf, instance)
        execute(kraken.update_eng_instance_conf, instance)
    #once all has been updated, we restart all services for the conf to be taken into account
    execute(tyr.restart_tyr_worker)
    execute(tyr.restart_tyr_beat)
    execute(jormungandr.reload_jormun_safe_all)
    execute(kraken.restart_all_krakens)

    # and we test the jormungandr
    for server in env.roledefs['ws']:
        jormungandr.test_jormungandr(utils.get_host_addr(server))

@task
def update_instance(instance):
    """
    param (instance) - update all configuration and restart all services
    does not deploy any packages
    """
    instance = utils.get_real_instance(instance)  # since it might be a endpoint we might need to get the real instance
    print(blue('updating {}'.format(instance.name)))
    #first of all we compute the instance status, it will be helpfull later
    execute(utils.compute_instance_status, instance)
    execute(tyr.create_tyr_instance, instance)
    execute(db.postgis_initdb, instance.db_name)
    execute(tyr.update_ed_db, instance.name)
    execute(jormungandr.deploy_jormungandr_instance_conf, instance)
    execute(kraken.create_eng_instance, instance)
    execute(tyr.deploy_default_synonyms, instance)

@task
def remove_instance(instance):
    """Completely remove all components for a given instance"""
    #TODO: deprecated, change with new instance handling
    execute(tyr.remove_ed_instance, instance)
    execute(db.remove_instance_from_jormun_database, instance)
    execute(kraken.remove_kraken_instance, instance)
    execute(tyr.remove_tyr_instance, instance)
    execute(jormungandr.remove_jormungandr_instance, instance)
    execute(tyr.remove_ed_instance, instance)  # @guikcd, done twice ?
    execute(remove_kraken_vip, instance)  # @guikcd only for not standalone no ?


# TODO: test all rename_*
@task
def rename_instance(current_instance, new_instance):
    """ Rename a given instance with new name
        to keep databases item authorization"""
    execute(tyr.stop_tyr_worker)
    execute(tyr.rename_tyr_instance, current_instance, new_instance)
    execute(jormungandr.remove_jormungandr_instance, current_instance)
    execute(tyr.remove_tyr_instance, current_instance)

    execute(db.rename_postgresql_database,
            db.instance2postgresql_name(current_instance),
            db.instance2postgresql_name(new_instance))
    execute(db.rename_tyr_jormungandr_database, current_instance, new_instance)

