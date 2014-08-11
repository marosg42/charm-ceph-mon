#!/usr/bin/python

#
# Copyright 2012 Canonical Ltd.
#
# Authors:
#  Paul Collins <paul.collins@canonical.com>
#  James Page <james.page@ubuntu.com>
#

import glob
import os
import shutil
import sys

import ceph
from charmhelpers.core.hookenv import (
    log, ERROR,
    config,
    relation_ids,
    related_units,
    relation_get,
    relation_set,
    remote_unit,
    Hooks, UnregisteredHookError,
    service_name
)

from charmhelpers.core.host import (
    service_restart,
    umount,
    mkdir,
    cmp_pkgrevno
)
from charmhelpers.fetch import (
    apt_install,
    apt_update,
    filter_installed_packages,
    add_source
)
from charmhelpers.payload.execd import execd_preinstall
from charmhelpers.contrib.openstack.alternatives import install_alternative
from charmhelpers.contrib.network.ip import (
    is_ipv6,
    get_ipv6_addr,
)

from utils import (
    render_template,
    get_public_addr,
)

hooks = Hooks()


def install_upstart_scripts():
    # Only install upstart configurations for older versions
    if cmp_pkgrevno('ceph', "0.55.1") < 0:
        for x in glob.glob('files/upstart/*.conf'):
            shutil.copy(x, '/etc/init/')


@hooks.hook('install')
def install():
    execd_preinstall()
    add_source(config('source'), config('key'))
    apt_update(fatal=True)
    apt_install(packages=ceph.PACKAGES, fatal=True)
    install_upstart_scripts()


def emit_cephconf():
    if config('prefer-ipv6'):
        host_ip = '[%s]' % get_ipv6_addr()
    #else:
    #    host_ip = '0.0.0.0'

    cephcontext = {
        'auth_supported': config('auth-supported'),
        'mon_hosts': ' '.join(get_mon_hosts()),
        'fsid': config('fsid'),
        'old_auth': cmp_pkgrevno('ceph', "0.51") < 0,
        'osd_journal_size': config('osd-journal-size'),
        'use_syslog': str(config('use-syslog')).lower(),
        'ceph_public_network': config('ceph-public-network'),
        'ceph_cluster_network': config('ceph-cluster-network'),
        'host_ip': host_ip,
    }
    # Install ceph.conf as an alternative to support
    # co-existence with other charms that write this file
    charm_ceph_conf = "/var/lib/charm/{}/ceph.conf".format(service_name())
    mkdir(os.path.dirname(charm_ceph_conf))
    with open(charm_ceph_conf, 'w') as cephconf:
        cephconf.write(render_template('ceph.conf', cephcontext))
    install_alternative('ceph.conf', '/etc/ceph/ceph.conf',
                        charm_ceph_conf, 100)

JOURNAL_ZAPPED = '/var/lib/ceph/journal_zapped'


@hooks.hook('config-changed')
def config_changed():
    log('Monitor hosts are ' + repr(get_mon_hosts()))

    # Pre-flight checks
    if not config('fsid'):
        log('No fsid supplied, cannot proceed.', level=ERROR)
        sys.exit(1)
    if not config('monitor-secret'):
        log('No monitor-secret supplied, cannot proceed.', level=ERROR)
        sys.exit(1)
    if config('osd-format') not in ceph.DISK_FORMATS:
        log('Invalid OSD disk format configuration specified', level=ERROR)
        sys.exit(1)

    emit_cephconf()

    e_mountpoint = config('ephemeral-unmount')
    if e_mountpoint and ceph.filesystem_mounted(e_mountpoint):
        umount(e_mountpoint)

    osd_journal = config('osd-journal')
    if (osd_journal and not os.path.exists(JOURNAL_ZAPPED)
            and os.path.exists(osd_journal)):
        ceph.zap_disk(osd_journal)
        with open(JOURNAL_ZAPPED, 'w') as zapped:
            zapped.write('DONE')

    # Support use of single node ceph
    if (not ceph.is_bootstrapped() and int(config('monitor-count')) == 1):
        ceph.bootstrap_monitor_cluster(config('monitor-secret'))
        ceph.wait_for_bootstrap()

    if ceph.is_bootstrapped():
        for dev in get_devices():
            ceph.osdize(dev, config('osd-format'), config('osd-journal'),
                        reformat_osd())
        ceph.start_osds(get_devices())


def get_mon_hosts():
    hosts = []
    addr = get_public_addr()
    if is_ipv6(addr):
        hosts.append('[{}]:6789'.format(addr))
    else:
        hosts.append('{}:6789'.format(addr))

    for relid in relation_ids('mon'):
        for unit in related_units(relid):
            if config('prefer-ipv6'):
                addr = relation_get('ceph-public-address', unit, relid)
            else:
                addr = relation_get('private-address', unit, relid)

            if addr is not None:
                if is_ipv6(addr):
                    hosts.append('[{}]:6789'.format(addr))
                else:
                    hosts.append('{}:6789'.format(addr))

    hosts.sort()
    return hosts


def reformat_osd():
    if config('osd-reformat'):
        return True
    else:
        return False


def get_devices():
    if config('osd-devices'):
        return config('osd-devices').split(' ')
    else:
        return []


@hooks.hook('mon-relation-joined')
def mon_relation_joined():
    for relid in relation_ids('mon'):
        relation_set(relation_id=relid,
                     relation_settings={'ceph-public-address':
                                        get_public_addr()})


@hooks.hook('mon-relation-departed',
            'mon-relation-changed')
def mon_relation():
    emit_cephconf()
    
    if config('prefer-ipv6'):
        host = '[%s]' % get_ipv6_addr()
    else:
        host = unit_get('private-address')
    relation_data = {}
    relation_data['private-address'] = host
    relation_set(**relation_data)

    moncount = int(config('monitor-count'))
    if len(get_mon_hosts()) >= moncount:
        ceph.bootstrap_monitor_cluster(config('monitor-secret'))
        ceph.wait_for_bootstrap()
        for dev in get_devices():
            ceph.osdize(dev, config('osd-format'), config('osd-journal'),
                        reformat_osd())
        ceph.start_osds(get_devices())
        notify_osds()
        notify_radosgws()
        notify_client()
    else:
        log('Not enough mons ({}), punting.'
            .format(len(get_mon_hosts())))


def notify_osds():
    for relid in relation_ids('osd'):
        osd_relation(relid)


def notify_radosgws():
    for relid in relation_ids('radosgw'):
        radosgw_relation(relid)


def notify_client():
    for relid in relation_ids('client'):
        client_relation(relid)


def upgrade_keys():
    ''' Ceph now required mon allow rw for pool creation '''
    if len(relation_ids('radosgw')) > 0:
        ceph.upgrade_key_caps('client.radosgw.gateway',
                              ceph._radosgw_caps)
    for relid in relation_ids('client'):
        units = related_units(relid)
        if len(units) > 0:
            service_name = units[0].split('/')[0]
            ceph.upgrade_key_caps('client.{}'.format(service_name),
                                  ceph._default_caps)


@hooks.hook('osd-relation-joined')
def osd_relation(relid=None):
    if ceph.is_quorum():
        log('mon cluster in quorum - providing fsid & keys')
        data = {
            'fsid': config('fsid'),
            'osd_bootstrap_key': ceph.get_osd_bootstrap_key(),
            'auth': config('auth-supported'),
            'ceph-public-address': get_public_addr(),
        }
        relation_set(relation_id=relid,
                     relation_settings=data)
    else:
        log('mon cluster not in quorum - deferring fsid provision')


@hooks.hook('radosgw-relation-joined')
def radosgw_relation(relid=None):
    # Install radosgw for admin tools
    apt_install(packages=filter_installed_packages(['radosgw']))
    if ceph.is_quorum():
        log('mon cluster in quorum - providing radosgw with keys')
        data = {
            'fsid': config('fsid'),
            'radosgw_key': ceph.get_radosgw_key(),
            'auth': config('auth-supported'),
            'ceph-public-address': get_public_addr(),
        }
        relation_set(relation_id=relid,
                     relation_settings=data)
    else:
        log('mon cluster not in quorum - deferring key provision')

    if config('prefer-ipv6'):
        host = '[%s]' % get_ipv6_addr()
    else:
        host = unit_get('private-address')

    relation_data = {}
    relation_data['private-address'] = host
    relation_set(**relation_data)

    log('End radosgw-relation hook.')


@hooks.hook('client-relation-joined')
def client_relation(relid=None):
    if config('prefer-ipv6'):
        host = '[%s]' % get_ipv6_addr()
    else:
        host = unit_get('private-address')
    relation_data = {}
    relation_data['private-address'] = host
    relation_set(**relation_data)

    if ceph.is_quorum():
        log('mon cluster in quorum - providing client with keys')
        service_name = None
        if relid is None:
            service_name = remote_unit().split('/')[0]
        else:
            units = related_units(relid)
            if len(units) > 0:
                service_name = units[0].split('/')[0]
        if service_name is not None:
            data = {
                'key': ceph.get_named_key(service_name),
                'auth': config('auth-supported'),
                'ceph-public-address': get_public_addr(),
            }
            relation_set(relation_id=relid,
                         relation_settings=data)
    else:
        log('mon cluster not in quorum - deferring key provision')


@hooks.hook('upgrade-charm')
def upgrade_charm():
    emit_cephconf()
    apt_install(packages=filter_installed_packages(ceph.PACKAGES), fatal=True)
    install_upstart_scripts()
    ceph.update_monfs()
    upgrade_keys()
    mon_relation_joined()


@hooks.hook('start')
def start():
    # In case we're being redeployed to the same machines, try
    # to make sure everything is running as soon as possible.
    service_restart('ceph-mon-all')
    if ceph.is_bootstrapped():
        ceph.start_osds(get_devices())


if __name__ == '__main__':
    try:
        hooks.execute(sys.argv)
    except UnregisteredHookError as e:
        log('Unknown hook {} - skipping.'.format(e))
