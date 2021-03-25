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

from importlib import import_module
import re
import os

from fabric.api import env
from fabric.colors import yellow
from fabric.decorators import task
from fabric.operations import run

env.distrib = 'ubuntu14.04'
env.KRAKEN_USER = 'www-data'
env.TYR_USER = env.KRAKEN_USER

# local path to navitia packages
env.debian_packages_path = None

env.use_systemd = False

# General enable/disable uwsgi
env.uwsgi_enable = False

def service_path():
    """
    Use an adapted service path according to the OS
    """
    if env.use_systemd:
        return '/etc/systemd/system/'
    else:
        return '/etc/init.d/'
env.service_path = service_path


def service_name(service):
    """
    Use an adapted service name with the path according to the OS
    """
    if env.use_systemd:
        return env.service_path() + service + '.service'
    else:
        return env.service_path() + service
env.service_name = service_name

env.default_ssh_user = 'root'


def make_ssh_url(serv, *args):
    """
    given an url, returns user@url
    will return a list [user@url1, user@url2, ...] if serv is iterable
    or if more than one argument is given
    """
    if args:
        serv = [serv]
        serv.extend(args)
    elif isinstance(serv, basestring):
        return "{}@{}".format(env.default_ssh_user, serv)
    return [make_ssh_url(s) for s in serv]
env.make_ssh_url = make_ssh_url

env.KRAKEN_RABBITMQ_OK_PORT = 5672
env.KRAKEN_RABBITMQ_WRONG_PORT = 56722
env.PUPPET_RESTART_DELAY = 30
# max time (in s) that a kraken can take to restart
env.KRAKEN_RESTART_DELAY = 90
env.TYR_WORKER_START_DELAY = 10
env.APACHE_START_DELAY = 8
env.KRAKEN_START_ONLY_ONCE = True
env.KRAKEN_RESTART_SCHEME = 'parallel'  # possible values: 'parallel', 'serial'
env.TYR_START_ONLY_ONCE = True
env.APACHE_START_ONLY_ONCE = True

# ======= ZMQ configuration =======
# (jormun <--> kraken communication)
# You specify your kraken architecture here
#
# A-   single machines architectures
# A.1- file sockets (default)
# configuration:
env.use_zmq_socket_file = True
env.zmq_server = None
# A.2- true sockets
# configuration:
# env.use_zmq_socket_file = False
# env.zmq_server = 'localhost'
# additionally, a zmq socket port must be specified in add_instance(...)
#
# B-  multi machine architectures
# In all cases below, a zmq socket port must be specified in add_instance(...)
# B.1 each kraken on a single engine
# configuration:
# env.use_zmq_socket_file = False
# additionally, a single zmq address must be specified in add_instance(...)
# this address must be a member of env.rolesdef['eng']
# B-2 each kraken on all engines
# configuration:
# env.use_zmq_socket_file = False
# env.zmq_server = 'virtual_server_address'
# this address refers to a reverse proxy (load balancer)
# B-3 each kraken on a subset of machines
# configuration:
# env.use_zmq_socket_file = False
# env.zmq_server = 'virtual_server_address'
# this address refers to a reverse proxy (load balancer)
# additionally, a list of zmq addresses must be specified in add_instance(...)
# these addresses must be members of env.rolesdef['eng']

# get the port in kraken.ini zmq_socket directive
# ex.: "zmq_socket = tcp://*:1234"
env.KRAKEN_RE_PORT = re.compile('^tcp://.*\:(?P<port>[0-9]*)')

# kraken.ini defaults number of thread for an instance
env.KRAKEN_NB_THREADS = 4
env.KRAKEN_START_PORT = 30000

env.AT_BASE_LOGDIR = '/var/log/connectors-rt'

env.ADC_HOSTNAME = 'pa4-adc1-prd.canaltp.prod'

""" default environment variables
    /!\ To overwrite these defaults, define it in the corresponding env below
    not here !
"""
# manual_package_deploy is used if we want to deploy custom debian package
# if false we only update the package from the debian repository
env.manual_package_deploy = False

# backup all configuration files before uploading a new one
env.backup_conf_files = False

#postgis dir is usefull for old postgis version where we cannot  do a 'create extention'
env.postgis_dir = '/usr/share/postgresql/9.1/contrib/postgis-1.5'

# if only one machine host all services
# only overriden to False on production and exploit envs
env.standalone = True
env.use_load_balancer = False
# some envronment use nfsv4 which does not allow chmod
env.use_nfs4 = False
# show diff when uploading a configuration file
env.show_diff_when_upload = 'diff'

# postgres host to ED database
env.ed_postgresql_database_host = 'localhost'
# postgres host to jormun/tyr database
env.tyr_postgresql_database_host = 'localhost'

def set_postgresql_database_host(db):
    # set all database hosts to 'db'
    env.ed_postgresql_database_host = db
    env.tyr_postgresql_database_host = db

env.set_postgresql_database_host = set_postgresql_database_host
# restart kraken
env.restart_service_always = False

# Rabbitmq connections parameters
#

# DEFAULT
env.rabbitmq_port_api = 15672

# KRAKEN
# used in the templates/kraken/kraken.ini.jinja

env.rabbitmq_kraken_host = 'localhost'
env.rabbitmq_kraken_port = 5672
env.rabbitmq_kraken_user = 'guest'
env.rabbitmq_kraken_pass = 'guest'
env.rabbitmq_kraken_vhost = '/'
env.rabbitmq_kraken_exchange = 'navitia'

# TYR
# used in the templates/tyr/instance.ini.jinja

env.rabbitmq_tyr_host = 'localhost'
env.rabbitmq_tyr_port = 5672
env.rabbitmq_tyr_user = 'guest'
env.rabbitmq_tyr_pass = 'guest'
env.rabbitmq_tyr_vhost = '/'


# STATS 
# used in the templates/jormungandr/settings.py.jinja

env.rabbitmq_stats_host = 'localhost'
env.rabbitmq_stats_port = 5672
env.rabbitmq_stats_user = 'guest'
env.rabbitmq_stats_pass = 'guest'
env.rabbitmq_stats_vhost = '/'
env.rabbitmq_stats_exchange = 'stat_persistor_exchange_topic'

env.ws_hosts_1 = []
env.eng_hosts_1 = []
env.ws_hosts_2 = []
env.eng_hosts_2 = []

# tyr instance, jormungangr, and other database host
# no need to change this, works if the server is standalone or if it use a
# remote postgresql server

# redis host used by tyr and jormungandr
env.redis_host = 'localhost'
env.redis_port = 6379
env.redis_socket_timeout = 0.1

# ed/tyr base dir
env.ed_basedir = '/srv/ed'
env.ed_datadir = '/srv/data'

# shift applied to the zmq port of a kraken to compute the prometheus port
env.kraken_prometheus_shift = 10000

# defines additional jormungandr settings (KEY = value)
env.jormungandr_additional_settings = {}

# Apache
env.base_apache = '/etc/apache2/'
def apache_version():
    """
    Return the apache version
    """
    lines = run('apt-cache policy apache2').split('\n')
    try:
        installed = lines[1].strip().split()[-1]
    except IndexError:
        installed= None
    return float(installed.rsplit('.', 1)[0])
env.apache_version = apache_version

def apache_conf_path(service):
    """
    Use an adapted service name with the path according to the apache2 version
    """
    version = apache_version()
    if version < 2.4:
        return env.base_apache + 'conf.d/' + service
    else:
        return env.base_apache + 'conf-available/' + service + '.conf'
env.apache_conf_path = apache_conf_path


##############################
# jormungandr
##############################
env.jormungandr_base_dir = '/srv/jormungandr'
env.jormungandr_wsgi_file = os.path.join(env.jormungandr_base_dir,
                                         'jormungandr.wsgi')
env.jormungandr_settings_file = os.path.join(env.jormungandr_base_dir,
                                          'settings.py')

# apache conf is usually not handled by fabric, but when it is (for docker for example)
# we do not use virtualhost, so all is in conf.d
env.jormungandr_apache_config_file = '/etc/apache2/conf.d/jormungandr.conf'

# uWSGI
env.jormungandr_uwsgi_config_file = os.path.join(env.jormungandr_base_dir,
                                          'jormungandr.ini')

env.jormungandr_newrelic_config_file = os.path.join(env.jormungandr_base_dir, 'newrelic.ini')

env.jormungandr_url = 'localhost'
env.jormungandr_url_prefix = ''
env.jormungandr_port = 80
env.jormungandr_listen_port = env.jormungandr_port
env.jormungandr_save_stats = True
env.jormungandr_is_public = False

env.jormungandr_log_dir = '/var/log/jormungandr/'
env.jormungandr_log_file = os.path.join(env.jormungandr_log_dir, 'jormungandr.log')
env.jormungandr_instances_dir = '/etc/jormungandr.d'
env.jormungandr_log_level = 'INFO'

# index of the redis data base used (integer from 0 to 15)
env.jormungandr_redis_db = 0
env.jormungandr_redis_user = None
env.jormungandr_redis_password = None
env.jormungandr_redis_implementation = 'redis'

env.jormungandr_enable_redis = False
env.jormungandr_cache_timeout = 600

env.jormungandr_use_uwsgi_cache = False

# stat_persitor and rt (?)
env.jormungandr_broker_username = 'guest'
env.jormungandr_broker_password = 'guest'

# jormungandr: kraken instance socket:
# [instance]
# key = bretagne
# socket = tcp://10.93.4.63:30001
# Note: only production need to override this
env.jormungandr_instance_socket = 'localhost'

env.jormungandr_default_handler = 'default'
env.jormungandr_syslog_facility = 'local7'
env.jormungandr_syslog_formatter = 'syslog'
env.jormungandr_log_format = '[%(asctime)s] [%(request_id)s] [%(levelname)5s] [%(process)5s] [%(name)10s] %(message)s'
env.jormungandr_log_extras = ''

#used for limiting the number of connections to the database
#by default we keep the default values
env.jormungandr_pool_size = None

env.jormungandr_timeo_cache_timeout = 60
env.jormungandr_siri_cache_timeout = 60
env.jormungandr_bss_provider = None

env.sqlalchemy_pool = None

env.newrelic_key = None
env.newrelic_app_name = ""

##############################
# kraken
##############################

# base dir for kraken binary and configuration
env.kraken_basedir = '/srv/kraken'

# log dir for all kraken instances, no need to change this
env.kraken_log_basedir = '/var/log/kraken'

# name of kraken database, no need to change this
env.kraken_data_nav = 'data.nav.lz4'

# log parameters
#
env.kraken_log_level = 'INFO'

env.kraken_log_max_backup = 5
# after this size (in MB), rotation done automatically by log4cplus lib
env.kraken_log_max_size = 20

# only used if syslog mode used instead of file
env.kraken_syslog_facility = 'local7'
env.kraken_syslog_ident = 'kraken'

# We use apache wsgi to monitor kraken
env.kraken_monitor_port = 80
env.kraken_monitor_location_dir = 'monitor-kraken'
env.kraken_monitor_listen_port = env.kraken_monitor_port
env.kraken_monitor_basedir = '/srv/monitor'
env.kraken_monitor_wsgi_file = os.path.join(env.kraken_monitor_basedir, 'monitor.wsgi')
env.kraken_monitor_config_file = os.path.join(env.kraken_monitor_basedir, 'settings.py')

env.feed_publisher = False

##############################
# tyr
##############################


env.tyr_base_instances_dir = '/etc/tyr.d'
env.tyr_basedir = '/srv/tyr'
env.tyr_base_logdir = '/var/log/tyr'
env.tyr_base_logfile = os.path.join(env.tyr_base_logdir, 'tyr.log')
env.tyr_logfile_pattern = os.path.join(env.tyr_base_logdir, '%(name).log')
env.tyr_migration_dir = os.path.join(env.tyr_basedir, 'migrations')
env.tyr_settings_file = os.path.join(env.tyr_basedir, 'settings.py')
env.tyr_settings_file_sh = os.path.join(env.tyr_basedir, 'settings.sh')
env.tyr_wsgi_file = os.path.join(env.tyr_basedir, 'settings.wsgi')
env.tyr_uwsgi_config_file = os.path.join(env.tyr_basedir, 'tyr.ini')

env.tyr_ws_url = 'localhost'
env.tyr_ws_port = 86

env.use_syslog = True
env.tyr_default_handler = 'default'
env.tyr_default_handler_instance = 'instance'
env.tyr_syslog_facility = 'local7'

env.use_json_log = False

# tyr share the same database as jormungandr
env.tyr_postgresql_database = 'jormungandr'
env.tyr_postgresql_user = 'jormungandr'
env.tyr_postgresql_password = 'jormungandr'


# variables for read_only user
env.postgres_read_only_user = 'read_only'
env.postgres_read_only_password = 'read_only'
env.postgres_schemas = ['georef', 'public', 'navitia']

# kirin
env.kirin_timeout = 60000

# redis
env.tyr_redis_password = None
# index of the database use in redis, between 0 and 15 by default
env.tyr_redis_db = 0

# smtp checks are not really possible when the server don't have direct smtp
# access (which should be always the case)
env.tyr_check_mx = False
env.tyr_check_smtp = False

#token for fabric to be able to query jormungandr
env.token = ''

#parameter to allow fabric to kill old tyr worker still alive after service stop
env.kill_ghost_tyr_worker = True

# default is to do things
env.dry_run = False

#number of parallele binarization
env.nb_thread_for_bina = 1
env.acceptable_bina_fail_rate = 0.08

#instances configurations
env.instances = {}

# those 3 strings template will be formated with base = tyr base directory and instance = name of the instance
env.tyr_backup_dir_template = '{base}/{instance}/backup'
env.tyr_source_dir_template = '{base}/{instance}/source'
env.tyr_destination_dir_template = '{base}/destination'

env.kraken_database_file = '{base_dest}/{instance}/data.nav.lz4'

#in general we don't want to configure apache
env.setup_apache = False

env.use_cities = True
env.cities_db_name = 'cities'
env.cities_db_password = 'password'
env.cities_db_user = 'navitia'
env.cities_path = '/srv/ed/backup/' # path where the cities file is archived

#use protobuff cpp implementation (needs up to date probobuf version (at least 2.6))
env.use_protobuf_cpp = False

env.is_prod = False

# exclude an instance if there is no existing data.nav.lz4
# see backup_datanav()tasks.tyr.backup_datanav
env.excluded_instances = []

# url of the autocomplete service
env.mimir_url = None
#url of bragi, the autocomplete service over mimir
env.bragi_url = None
env.bragi_timeout = 2

# use for supervision
env.supervision_handler = None
env.supervision_config = dict(
    tyr_beat=dict(
        downtime=0,
        hosts=[],
        service=''
    ),
    bina=dict(
        downtime=0,
        hosts=[],
        service=''
    ),
    kraken=dict(
        downtime=0,
        hosts=[],
        service=''
    )
)

@task
def let(**kwargs):
    """
    This function is a way to give env variables in the cli

    call then

    fab dev let:x=bob,z=toto upgrade_all

    to have a env.x and env.z variable
    """
    env.update(kwargs)


@task
def really_run():
    """ If called set dry_run as false """
    print(yellow("WARNING: really_run() is now deprecated and dry_run is always"
        "set to 'False'. Use dry_run() to set it to 'True'"))

def dry_run():
    """ If called set dry_run as true """
    env.dry_run = True


""" Environnements """

@task
def use(module_path, *args):
    pos = module_path.rfind(".")
    if pos == -1:
        path, f_name = module_path, module_path
    else:
        path, f_name = module_path[:pos], module_path[pos+1:]
    module = import_module(path)
    getattr(module, f_name)(*args)


""" Include or exclude instances """

@task
def include(*args):
    """
    include only the listed instances when running a fabric command
    :param args: instance names, as of 1st parameter of add_instance()
    """
    for name in args:
        if name not in env.instances:
            raise ValueError("Unknown instance '{}'".format(name))
    for name in list(env.instances):
        if not name in args:
            del env.instances[name]


@task
def exclude(*args):
    """
    exclude all the listed instances when running a fabric command
    :param args: instance names, as of 1st parameter of add_instance()
    """
    for name in args:
        try:
            del env.instances[name]
        except KeyError:
            raise ValueError("Unknown instance '{}'".format(name))
