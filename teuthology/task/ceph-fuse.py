import contextlib
import logging
import os

from teuthology import misc as teuthology
from ..orchestra import run
from gevent.timeout import Timeout

log = logging.getLogger(__name__)

def _fuse_unmount(remote, mnt):
    remote.run(
        args=[
            'fusermount',
            '-u',
            mnt,
            ],
        )

def _abort_fuse_mount(remote, mnt):
    # abort the fuse mount, killing all hung processes
    remote.run(
        args=[
            'echo',
            '1',
            run.Raw('>'),
            run.Raw('/sys/fs/fuse/connections/*/abort'),
            ],
        )
    # make sure its unmounted
    remote.run(
        args=[
            'sudo',
            'umount',
            '-l',
            '-f',
            mnt,
            ],
        )

def _wait_for_daemons(remote, fuse_daemons, timeout=None):
    # make sure we wait even after the process is killed
    done = False
    while not done:
        try:
            run.wait(fuse_daemons.itervalues(), timeout=timeout)
            done = True
        except Timeout:
            log.error('fuse client did not exit after {timeout} seconds'.format(timeout=timeout))
            log.error('sending SIGKILL to ceph-fuse process')
            # kill the fuse client
            # todo: figure out how to get the process ids from the
            # fuse_daemons
            remote.run(
                args=[
                    'sudo',
                    'killall',
                    '-9',
                    'ceph-fuse',
                    ],
                )

@contextlib.contextmanager
def task(ctx, config):
    """
    Mount/unmount a ``ceph-fuse`` client.

    The config is optional and defaults to mounting on all clients. If
    a config is given, it is expected to be a list of clients to do
    this operation on. This lets you e.g. set up one client with
    ``ceph-fuse`` and another with ``kclient``.

    Example that mounts all clients::

        tasks:
        - ceph:
        - ceph-fuse:
        - interactive:

    Example that uses both ``kclient` and ``ceph-fuse``::

        tasks:
        - ceph:
        - ceph-fuse: [client.0]
        - kclient: [client.1]
        - interactive:

    Example that enables valgrind:

        tasks:
        - ceph:
        - ceph-fuse:
            client.0:
              valgrind: [--tool=memcheck, --leak-check=full, --show-reachable=yes]
        - interactive:

    Example that sets a timeout (in seconds) for the ceph-fuse process to finish after unmount:

        tasks:
        - ceph:
        - ceph-fuse:
            client.0:
              timeout: 300
        - interactive:

    Example that performs cleanup even if the fuse process fails:

        tasks:
        - ceph:
        - ceph-fuse:
            client.0:
              cleanup_on_failure: true
        - interactive:

    """
    log.info('Mounting ceph-fuse clients...')
    fuse_daemons = {}

    if config is None:
        config = dict(('client.{id}'.format(id=id_), None)
                  for id_ in teuthology.all_roles_of_type(ctx.cluster, 'client'))
    elif isinstance(config, list):
        config = dict((name, None) for name in config)

    overrides = ctx.config.get('overrides', {})
    teuthology.deep_merge(config, overrides.get('ceph-fuse', {}))

    clients = list(teuthology.get_clients(ctx=ctx, roles=config.keys()))

    for id_, remote in clients:
        mnt = os.path.join('/tmp/cephtest', 'mnt.{id}'.format(id=id_))
        log.info('Mounting ceph-fuse client.{id} at {remote} {mnt}...'.format(
                id=id_, remote=remote,mnt=mnt))

        client_config = config.get("client.%s" % id_)
        if client_config is None:
            client_config = {}
        log.info("Client client.%s config is %s" % (id_, client_config))

        daemon_signal = 'kill'
        if client_config.get('coverage') or client_config.get('valgrind') is not None:
            daemon_signal = 'term'

        remote.run(
            args=[
                'mkdir',
                '--',
                mnt,
                ],
            )

        run_cmd=[
            '/tmp/cephtest/enable-coredump',
            '/tmp/cephtest/binary/usr/local/bin/ceph-coverage',
            '/tmp/cephtest/archive/coverage',
            '/tmp/cephtest/daemon-helper',
            daemon_signal,
            ]
        run_cmd_tail=[
            '/tmp/cephtest/binary/usr/local/bin/ceph-fuse',
            '-f',
            '--name', 'client.{id}'.format(id=id_),
            '-c', '/tmp/cephtest/ceph.conf',
            # TODO ceph-fuse doesn't understand dash dash '--',
            mnt,
            ]

        if client_config.get('valgrind') is not None:
            run_cmd.extend(
                teuthology.get_valgrind_args(
                    'client.{id}'.format(id=id_),
                    client_config.get('valgrind'),
                    )
                )

        run_cmd.extend(run_cmd_tail)

        proc = remote.run(
            args=run_cmd,
            logger=log.getChild('ceph-fuse.{id}'.format(id=id_)),
            stdin=run.PIPE,
            wait=False,
            )
        fuse_daemons[id_] = proc

    for id_, remote in clients:
        mnt = os.path.join('/tmp/cephtest', 'mnt.{id}'.format(id=id_))
        teuthology.wait_until_fuse_mounted(
            remote=remote,
            fuse=fuse_daemons[id_],
            mountpoint=mnt,
            )

    try:
        yield
    finally:
        log.info('Unmounting ceph-fuse clients...')
        for id_, remote in clients:
            client_config = config.get("client.%s" % id_)
            if client_config is None:
                client_config = {}
            fuse_timeout = client_config.get('timeout')
            cleanup_on_failure = client_config.get('cleanup_on_failure', False)

            mnt = os.path.join('/tmp/cephtest', 'mnt.{id}'.format(id=id_))
            try:
                _fuse_unmount(remote, mnt)
            except run.CommandFailedError as e:
                if not cleanup_on_failure:
                    raise
                log.error('fuse failed to unmount, cleaning up anyway')
                _abort_fuse_mount(remote, mnt)

            try:
                _wait_for_daemons(remote, fuse_daemons, timeout=fuse_timeout)
            except run.CommandFailedError as e:
                if not cleanup_on_failure:
                    raise
                log.info('ceph-fuse process failed, cleaning up anyway')

        for id_, remote in clients:
            mnt = os.path.join('/tmp/cephtest', 'mnt.{id}'.format(id=id_))
            remote.run(
                args=[
                    'rmdir',
                    '--',
                    mnt,
                    ],
                )
