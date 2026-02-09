import asyncio
import kopf
import logging
import unittest

from kubernetes.client import V1ObjectMeta, V1Secret, ApiException
from unittest.mock import ANY, Mock, patch

from handlers import create_fn, custom_objects_api, csecs_cache, namespace_watcher, on_field_data, startup_fn, on_secret_event
from kubernetes_utils import create_secret_metadata
from models import BaseClusterSecret


class TestClusterSecretHandler(unittest.TestCase):

    def setUp(self):
        self.logger = logging.getLogger(__name__)
        for cluster_secret in csecs_cache.all_cluster_secret():
            csecs_cache.remove_cluster_secret(cluster_secret.uid)

    def test_on_field_data_cache(self):
        """New data should be written into the cache.
        """

        # Old data in the cache.
        csec = BaseClusterSecret(
            uid="mysecretuid",
            name="mysecret",
            metadata={"name": "mysecret", "uid": "mysecretuid"},
            data={"key": "oldvalue"},
            synced_namespace=[],
        )

        csecs_cache.set_cluster_secret(csec)

        # New data coming into the callback.
        new_body = {"metadata": {"name": "mysecret", "uid": "mysecretuid"}, "data": {"key": "newvalue"}}

        on_field_data(
            old={"key": "oldvalue"},
            new={"key": "newvalue"},
            body=new_body,
            meta=kopf.Meta({"metadata": {"name": "mysecret"}}),
            name="mysecret",
            uid="mysecretuid",
            logger=self.logger,
            reason="update",
        )

        # New data should be in the cache.
        self.assertEqual(
            csecs_cache.get_cluster_secret("mysecretuid").data,
            {"key": "newvalue"},
        )

    def test_on_field_data_sync(self):
        """Must sync secret data changes to the namespaces.
        """

        mock_v1 = Mock()

        # Old data in the namespaced secret of the myns namespace.
        mock_v1.read_namespaced_secret.return_value = V1Secret(
            api_version='v1',
            data={"key": "oldvalue"},
            kind='Secret',
            metadata=create_secret_metadata(
                name="mysecret",
                namespace="myns",
            ),
            type="Opaque",
        )

        # Old data in the cache.
        csec = BaseClusterSecret(
            uid="mysecretuid",
            name="mysecret",
            metadata={
                "name": "mysecret", 
                "uid": "mysecretuid",
            },
            data={"key": "oldvalue"},
            synced_namespace=["myns"],
        )

        csecs_cache.set_cluster_secret(csec)

        # New data coming into the callback.
        new_body = {
            "metadata": {"name": "mysecret", "uid": "mysecretuid"},
            "data": {"key": "newvalue"},
            "status": {"create_fn": {"syncedns": ["myns"]}},
        }

        with patch("handlers.v1", mock_v1):
            on_field_data(
                old={"key": "oldvalue"},
                new={"key": "newvalue"},
                body=new_body,
                meta=kopf.Meta({"metadata": {"name": "mysecret"}}),
                name="mysecret",
                uid="mysecretuid",
                logger=self.logger,
                reason="update",
            )

        # Namespaced secret should be updated.
        mock_v1.replace_namespaced_secret.assert_called_once_with(
            name=csec.name,
            namespace="myns",
            body=ANY,
        )

        # Namespaced secret should be updated with the new data.
        self.assertEqual(
            mock_v1.replace_namespaced_secret.call_args.kwargs.get("body").data,
            {"key": "newvalue"},
        )

    def test_create_fn(self):
        """Namespace name must be correct in the cache.
        """

        mock_v1 = Mock()

        body = {
            "metadata": {
                "name": "mysecret",
                "uid": "mysecretuid"
            },
            "data": {"key": "value"}
        }

        # Define the predefined list of namespaces you want to use in the test
        predefined_nss = [Mock(metadata=V1ObjectMeta(name=ns)) for ns in ["default", "myns"]]

        # Configure the mock's behavior to return the predefined namespaces when list_namespace is called
        mock_v1.list_namespace.return_value.items = predefined_nss

        with patch("handlers.v1", mock_v1), \
             patch("handlers.sync_secret"):
            asyncio.run(
                create_fn(
                    logger=self.logger,
                    uid="mysecretuid",
                    name="mysecret",
                    body=body,
                )
            )

        # The secrets should be in all namespaces of the cache.
        self.assertEqual(
            csecs_cache.get_cluster_secret("mysecretuid").synced_namespace,
            ["default", "myns"],
        )

    def test_ns_create(self):
        """A new namespace must get the cluster secrets.
        """

        mock_v1 = Mock()

        # Define the predefined list of namespaces you want to use in the test
        predefined_nss = [Mock(metadata=V1ObjectMeta(name=ns)) for ns in ["default", "myns"]]

        # Configure the mock's behavior to return the predefined namespaces when list_namespace is called
        mock_v1.list_namespace.return_value.items = predefined_nss

        patch_clustersecret_status = Mock()

        csec = BaseClusterSecret(
            uid="mysecretuid",
            name="mysecret",
            metadata={"name": "mysecret"},
            data={"key": "mydata"},
            synced_namespace=["default"],
        )

        csecs_cache.set_cluster_secret(csec)

        with patch("handlers.v1", mock_v1), \
             patch("handlers.patch_clustersecret_status", patch_clustersecret_status):
            asyncio.run(
                namespace_watcher(
                    logger=self.logger,
                    meta=kopf.Meta({"metadata": {"name": "myns"}}),
                    reason="create",
                )
            )

        # The new namespace should have the secret copied into it.
        mock_v1.replace_namespaced_secret.assert_called_once_with(
            name=csec.name,
            namespace="myns",
            body=ANY,
        )

        # The namespace should be added to the syncedns status of the clustersecret.
        patch_clustersecret_status.assert_called_once_with(
            logger=self.logger,
            name=csec.name,
            new_status={'create_fn': {'syncedns': ["default", "myns"]}},
            custom_objects_api=custom_objects_api,
        )

        # The new namespace should be in the cache.
        self.assertCountEqual(
            csecs_cache.get_cluster_secret("mysecretuid").synced_namespace,
            ["default", "myns"],
        )

    def test_ns_delete(self):
        """Deleted namespace must be removed from cluster secret 'status.create_fn.syncedns' filed.
        """

        mock_v1 = Mock()

        # Define the predefined list of namespaces you want to use in the test (after namespace deletion)
        predefined_nss = [Mock(metadata=V1ObjectMeta(name=ns)) for ns in ["default"]]

        # Configure the mock's behavior to return the predefined namespaces when list_namespace is called
        mock_v1.list_namespace.return_value.items = predefined_nss

        patch_clustersecret_status = Mock()

        # The list of synced namespaces here are before namespace deletion handler is called
        csec = BaseClusterSecret(
            uid="mysecretuid",
            name="mysecret",
            metadata={"name": "mysecret"},
            data={"key": "mydata"},
            synced_namespace=["default", "myns"],
        )

        csecs_cache.set_cluster_secret(csec)

        with patch("handlers.v1", mock_v1), \
             patch("handlers.patch_clustersecret_status", patch_clustersecret_status):
            asyncio.run(
                namespace_watcher(
                    logger=self.logger,
                    meta=kopf.Meta({"metadata": {"name": "myns"}}),
                    reason="delete",
                )
            )

        # The syncedns status of the clustersecret should not contains deleted namespace.
        patch_clustersecret_status.assert_called_once_with(
            logger=self.logger,
            name=csec.name,
            new_status={'create_fn': {'syncedns': ["default"]}},
            custom_objects_api=custom_objects_api,
        )

        # The deleted namespace should not be in the cache.
        self.assertCountEqual(
            csecs_cache.get_cluster_secret("mysecretuid").synced_namespace,
            ["default"],
        )

    def test_startup_fn(self):
        """Must not fail on empty namespace in ClusterSecret metadata (it's cluster-wide after all).
        """

        get_custom_objects_by_kind = Mock()

        csec = BaseClusterSecret(
            uid="mysecretuid",
            name="mysecret",
            metadata={"name": "mysecret", "uid": "mysecretuid"},
            data={"key": "mydata"},
            synced_namespace=[],
        )

        get_custom_objects_by_kind.return_value = [{
            "metadata": csec.metadata,
            "data": csec.data,
            "status": {"create_fn": {"syncedns": []}}
        }]

        with patch("handlers.get_custom_objects_by_kind", get_custom_objects_by_kind):
            asyncio.run(startup_fn(logger=self.logger))

        # The secret should be in the cache.
        self.assertEqual(
            csecs_cache.get_cluster_secret("mysecretuid").uid,
            csec.uid,
        )

    def test_on_secret_change(self):
        """Must sync changes from source secret to target namespaces.
        """
        mock_v1 = Mock()
        sync_secret_mock = Mock()

        # ClusterSecret using valueFrom
        csec = BaseClusterSecret(
            uid="csec-uid",
            name="csec-name",
            metadata={"name": "csec-name", "uid": "csec-uid"},
            data={
                "valueFrom": {
                    "secretKeyRef": {
                        "name": "source-secret",
                        "namespace": "source-ns"
                    }
                }
            },
            synced_namespace=["target-ns"],
        )
        csecs_cache.set_cluster_secret(csec)

        event = {
            'type': 'MODIFIED',
            'object': {
                'metadata': {
                    'name': 'source-secret',
                    'namespace': 'source-ns',
                    'labels': {}
                }
            }
        }

        with patch("handlers.v1", mock_v1), \
             patch("handlers.sync_secret", sync_secret_mock):
            on_secret_event(
                event=event,
                logger=self.logger
            )

        # Should trigger sync for the target namespace
        expected_body = {
            'metadata': csec.metadata,
            'data': csec.data,
            'type': csec.type
        }
        sync_secret_mock.assert_called_once_with(
            self.logger, "target-ns", expected_body, mock_v1
        )

    def test_on_managed_secret_delete(self):
        """Must restore deleted managed secrets (self-healing).
        """
        mock_v1 = Mock()
        sync_secret_mock = Mock()
        from consts import CREATE_BY_ANNOTATION, CREATE_BY_AUTHOR

        csec = BaseClusterSecret(
            uid="csec-uid",
            name="managed-secret",
            metadata={"name": "managed-secret", "uid": "csec-uid"},
            data={"key": "value"},
            synced_namespace=["target-ns"],
        )
        csecs_cache.set_cluster_secret(csec)

        event = {
            'type': 'DELETED',
            'object': {
                'metadata': {
                    'name': 'managed-secret',
                    'namespace': 'target-ns',
                    'annotations': {CREATE_BY_ANNOTATION: CREATE_BY_AUTHOR},
                    'labels': {}
                }
            }
        }

        # Mock read_namespace to return non-terminating
        mock_ns = Mock()
        mock_ns.status.phase = 'Active'
        mock_v1.read_namespace.return_value = mock_ns

        with patch("handlers.v1", mock_v1), \
             patch("handlers.sync_secret", sync_secret_mock):
            on_secret_event(
                event=event,
                logger=self.logger
            )

        # Should trigger sync to restore
        expected_body = {
            'metadata': csec.metadata,
            'data': csec.data,
            'type': csec.type
        }
        sync_secret_mock.assert_called_once_with(
            self.logger, "target-ns", expected_body, mock_v1
        )

    def test_on_source_secret_delete_warning(self):
        """Must log a warning when a source secret is deleted.
        """
        mock_v1 = Mock()
        logger_mock = Mock()

        csec = BaseClusterSecret(
            uid="csec-uid",
            name="csec-name",
            metadata={"name": "csec-name", "uid": "csec-uid"},
            data={
                "valueFrom": {
                    "secretKeyRef": {
                        "name": "source-secret",
                        "namespace": "source-ns"
                    }
                }
            },
            synced_namespace=["target-ns"],
        )
        csecs_cache.set_cluster_secret(csec)

        event = {
            'type': 'DELETED',
            'object': {
                'metadata': {
                    'name': 'source-secret',
                    'namespace': 'source-ns',
                    'labels': {}
                }
            }
        }

        on_secret_event(
            event=event,
            logger=logger_mock
        )

        # Should log a warning
        logger_mock.warning.assert_called()
        self.assertIn("was deleted!", logger_mock.warning.call_args[0][0])
