import logging
import re
import sys
from typing import Any, Dict, List, Optional, Mapping

import kopf
from kubernetes import client, config
from kubernetes.client import exceptions

from cache import Cache, MemoryCache
from kubernetes_utils import delete_secret, get_ns_list, sync_secret, patch_clustersecret_status, get_custom_objects_by_kind
from consts import CREATE_BY_ANNOTATION, CREATE_BY_AUTHOR
from models import BaseClusterSecret

# In-memory dictionary for all ClusterSecrets in the Cluster. UID -> ClusterSecret Body
csecs_cache: Cache = MemoryCache()

from os_utils import in_cluster

if "unittest" not in sys.modules:
    # Loading kubeconfig
    if in_cluster():
        # Loading kubeconfig
        config.load_incluster_config()
    else:
        # Loading using the local kubevonfig.
        config.load_kube_config()

v1 = client.CoreV1Api()
custom_objects_api = client.CustomObjectsApi()


def is_noise_secret(name: str, labels: kopf.Labels) -> bool:
    """Returns True if the secret is considered 'noise' (Helm, GitLab Runner, etc.)
    """
    if name.startswith('sh.helm.release.v1.'):
        return True
    if labels.get('owner') == 'helm':
        return True
    if re.match(r'^runner-.*-project-.*-concurrent-.*$', name):
        return True
    return False


@kopf.on.delete('clustersecret.io', 'v1', 'clustersecrets')
def on_delete(
    body: Dict[str, Any],
    uid: str,
    name: str,
    logger: logging.Logger,
    **_,
):
    # Delete from memory FIRST to prevent self-healing race
    try:
        csecs_cache.remove_cluster_secret(uid)
        logger.debug(f'csec {uid} deleted from memory ok')
    except KeyError as k:
        logger.info(f'This csec was not found in memory, maybe it was created in another run: {k}')
    logger.debug(f'csec {uid} deleted from memory ok')

    syncedns = body.get('status', {}).get('create_fn', {}).get('syncedns', [])
    for ns in syncedns:
        delete_secret(logger, ns, name, v1)


@kopf.on.field('clustersecret.io', 'v1', 'clustersecrets', field='avoidNamespaces')
@kopf.on.field('clustersecret.io', 'v1', 'clustersecrets', field='matchNamespace')
def on_fields_avoid_or_match_namespace(
    old: Optional[List[str]],
    new: List[str],
    name: str,
    body,
    uid: str,
    logger: logging.Logger,
    reason: kopf.Reason,
    **_,
):
    if reason == "create":
        logger.debug('This is a new object: Ignoring.')
        return

    logger.debug(f'Avoid or match namespaces changed: {old} -> {new}')
    logger.debug(f'Updating Object body == {body}')

    syncedns = body.get('status', {}).get('create_fn', {}).get('syncedns', [])

    updated_matched = get_ns_list(logger, body, v1)
    to_add = set(updated_matched).difference(set(syncedns))
    to_remove = set(syncedns).difference(set(updated_matched))

    logger.debug(f'Add secret to namespaces: {to_add}, remove from: {to_remove}')

    for secret_namespace in to_add:
        sync_secret(logger, secret_namespace, body, v1)

    for secret_namespace in to_remove:
        delete_secret(logger, secret_namespace, name, v1)

    cached_cluster_secret = csecs_cache.get_cluster_secret(uid)
    if cached_cluster_secret is None:
        logger.error('Received an event for an unknown ClusterSecret.')

    # Updating the cache
    csecs_cache.set_cluster_secret(BaseClusterSecret(
        uid=uid,
        name=name,
        data=body.get('data'),
        metadata=body.get('metadata'),
        synced_namespace=updated_matched,
        type=body.get('type', 'Opaque'),
        match_namespace=body.get('matchNamespace'),
        avoid_namespaces=body.get('avoidNamespaces'),
    ))

    # Patch synced_ns field
    logger.debug(f'Patching clustersecret {name}')
    patch_clustersecret_status(
        logger=logger,
        name=name,
        new_status={'create_fn': {'syncedns': updated_matched}},
        custom_objects_api=custom_objects_api,
    )


@kopf.on.field('clustersecret.io', 'v1', 'clustersecrets', field='data')
def on_field_data(
    old: Dict[str, str],
    new: Dict[str, str],
    body: Dict[str, Any],
    name: str,
    uid: str,
    logger: logging.Logger,
    reason: kopf.Reason,
    **_,
):
    if reason == "create":
        logger.debug('This is a new object: Ignoring')
        return

    logger.debug(f'Data changed: {old} -> {new}')
    logger.debug(f'Updating Object body == {body}')
    syncedns = body.get('status', {}).get('create_fn', {}).get('syncedns', [])

    cached_cluster_secret = csecs_cache.get_cluster_secret(uid)
    if cached_cluster_secret is None:
        logger.error('Received an event for an unknown ClusterSecret.')

    for ns in syncedns:
        logger.info(f'Re Syncing secret {name} in ns {ns}')
        sync_secret(logger, ns, body, v1)

    # Updating the cache
    csecs_cache.set_cluster_secret(BaseClusterSecret(
        uid=uid,
        name=name,
        data=body.get('data'),
        metadata=body.get('metadata'),
        synced_namespace=syncedns,
        type=body.get('type', 'Opaque'),
        match_namespace=body.get('matchNamespace'),
        avoid_namespaces=body.get('avoidNamespaces'),
    ))


@kopf.on.resume('clustersecret.io', 'v1', 'clustersecrets')
@kopf.on.create('clustersecret.io', 'v1', 'clustersecrets')
async def create_fn(
    logger: logging.Logger,
    uid: str,
    name: str,
    body: Dict[str, Any],
    **_
):
    # get all ns matching.
    matchedns = get_ns_list(logger, body, v1)

    # sync in all matched NS
    logger.info(f'Syncing on Namespaces: {matchedns}')
    for ns in matchedns:
        sync_secret(logger, ns, body, v1)

    # Updating the cache
    csecs_cache.set_cluster_secret(BaseClusterSecret(
        uid=uid,
        name=name,
        data=body.get('data'),
        metadata=body.get('metadata'),
        synced_namespace=matchedns,
        type=body.get('type', 'Opaque'),
        match_namespace=body.get('matchNamespace'),
        avoid_namespaces=body.get('avoidNamespaces'),
    ))

    # This return is mandatory! It's used to update the status of the CRD
    # https://kopf.readthedocs.io/en/stable/results/
    return {'syncedns': matchedns}


@kopf.on.create('', 'v1', 'namespaces')
@kopf.on.delete('', 'v1', 'namespaces')
async def namespace_watcher(logger: logging.Logger, reason: kopf.Reason, meta: kopf.Meta, **_):
    """Watch for namespace events
    """
    if reason not in ["create", "delete"]:
        logger.error(f'Function "namespace_watcher" was called with incorrect reason: {reason}')
        return
    
    ns_name = meta.name
    logger.info(f'Namespace {"created" if reason == "create" else "deleted"}: {ns_name}. Re-syncing')
    
    ns_list_new = []
    for cached_cluster_secret in csecs_cache.all_cluster_secret():
        # Reconstruct a minimal body for get_ns_list and sync_secret
        body = {
            'metadata': cached_cluster_secret.metadata,
            'data': cached_cluster_secret.data,
            'type': cached_cluster_secret.type,
            'matchNamespace': cached_cluster_secret.match_namespace,
            'avoidNamespaces': cached_cluster_secret.avoid_namespaces,
        }
        
        name = cached_cluster_secret.name
        ns_list_synced = cached_cluster_secret.synced_namespace
        ns_list_new = get_ns_list(logger, body, v1)
        ns_list_changed = False

        logger.debug(f'ClusterSecret: {name}. Old matched namespaces: {ns_list_synced}')
        logger.debug(f'ClusterSecret: {name}. New matched namespaces: {ns_list_new}')
        
        if reason == "create" and ns_name in ns_list_new:
            logger.info(f'Cloning secret {name} into the new namespace: {ns_name}')
            sync_secret(
                logger=logger,
                namespace=ns_name,
                body=body,
                v1=v1,
            )
            ns_list_changed = True
        
        if reason == "delete" and ns_name in ns_list_synced:
            logger.info(f'Secret {name} removed from deleted namespace: {ns_name}')
            # Ensure that deleted namespace will not come in new list - on moment when this event handled by kopf the namespace in kubernetes can still exists
            if ns_name in ns_list_new:
                ns_list_new.remove(ns_name)
            ns_list_changed = True

        # Update ClusterSecret only if there are changes in list of his namespaces
        if ns_list_changed:
            # Update in-memory cache
            cached_cluster_secret.synced_namespace = ns_list_new
            csecs_cache.set_cluster_secret(cached_cluster_secret)

            # Update the list of synced namespaces in kubernetes object
            logger.debug(f'Patching ClusterSecret: {name}')
            patch_clustersecret_status(
                logger=logger,
                name=name,
                new_status={'create_fn': {'syncedns': ns_list_new}},
                custom_objects_api=custom_objects_api,
            )
        else:
            logger.debug(f'There are no changes in the list of namespaces for ClusterSecret: {name}')

@kopf.on.event('', 'v1', 'secrets')
def on_secret_event(event, logger: logging.Logger, **_):
    """Watch for all secret events
    """
    event_type = event.get('type')
    obj = event.get('object')
    if not obj or event_type not in ['ADDED', 'MODIFIED', 'DELETED']:
        return
        
    metadata = obj.get('metadata', {})
    name = metadata.get('name')
    namespace = metadata.get('namespace')
    labels = metadata.get('labels', {})
    annotations = metadata.get('annotations', {})

    if is_noise_secret(name, labels):
        return

    # Check if this secret is relevant to any ClusterSecret
    # A secret is relevant if:
    # 1. It is managed by us (has our annotation)
    # 2. It is a source secret (matches name/namespace of any valueFrom)
    
    is_managed = annotations.get(CREATE_BY_ANNOTATION) == CREATE_BY_AUTHOR
    
    # Pre-calculate if it's a source secret to avoid repeated loops
    source_for_csecs = []
    for cached_cluster_secret in csecs_cache.all_cluster_secret():
        value_from = cached_cluster_secret.data.get('valueFrom', {})
        ref = value_from.get('secretKeyRef', {})
        if ref.get('name') == name and ref.get('namespace') == namespace:
            source_for_csecs.append(cached_cluster_secret)

    if not is_managed and not source_for_csecs:
        return

    # 1. Handle Secret Creation or Update (Source Secret tracking)
    if event_type in ['ADDED', 'MODIFIED']:
        for csec in source_for_csecs:
            logger.info(f'Source secret {name} in namespace {namespace} changed. Re-syncing ClusterSecret {csec.name}')
            body = {
                'metadata': csec.metadata,
                'data': csec.data,
                'type': csec.type
            }
            for ns in csec.synced_namespace:
                logger.debug(f'Re-syncing ClusterSecret {csec.name} to namespace {ns}')
                sync_secret(logger, ns, body, v1)

    # 2. Handle Secret Deletion
    if event_type == 'DELETED':
        if is_managed:
            # Self-healing: restore if the ClusterSecret still exists and namespace is not terminating
            for cached_cluster_secret in csecs_cache.all_cluster_secret():
                if cached_cluster_secret.name == name and namespace in cached_cluster_secret.synced_namespace:
                    # Check if namespace is terminating
                    try:
                        ns = v1.read_namespace(name=namespace)
                        if ns.status.phase == 'Terminating':
                            logger.info(f'Namespace {namespace} is terminating. Skipping self-healing for secret {name}.')
                            return
                    except exceptions.ApiException as e:
                        if e.status == 404:
                            return
                        logger.error(f'Error checking namespace status: {e}')

                    logger.info(f'Managed secret {name} deleted from namespace {namespace}. Re-syncing to restore.')
                    body = {
                        'metadata': cached_cluster_secret.metadata,
                        'data': cached_cluster_secret.data,
                        'type': cached_cluster_secret.type
                    }
                    sync_secret(logger, namespace, body, v1)
                    return
        
        for csec in source_for_csecs:
            logger.warning(f'Source secret {name} in namespace {namespace} was deleted! ClusterSecret {csec.name} is now stale.')

@kopf.on.startup()
async def startup_fn(logger: logging.Logger, **_):
    logger.debug(
        """
      #########################################################################
      # DEBUG MODE ON - NOT FOR PRODUCTION                                    #
      # On this mode secrets are leaked to stdout, this is not safe!. NO-GO ! #
      #########################################################################
    """,
    )

    cluster_secrets = get_custom_objects_by_kind(
        group='clustersecret.io',
        version='v1',
        plural='clustersecrets',
        custom_objects_api=custom_objects_api,
    )

    logger.info(f'Found {len(cluster_secrets)} existing cluster secrets.')
    for item in cluster_secrets:
        metadata = item.get('metadata')
        csecs_cache.set_cluster_secret(
            BaseClusterSecret(
                uid=metadata.get('uid'),
                name=metadata.get('name'),
                data=item.get('data'),
                metadata=metadata,
                synced_namespace=item.get('status', {}).get('create_fn', {}).get('syncedns', []),
                type=item.get('type', 'Opaque'),
                match_namespace=item.get('matchNamespace'),
                avoid_namespaces=item.get('avoidNamespaces'),
            )
        )
