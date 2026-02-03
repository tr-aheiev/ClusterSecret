IMG_NAMESPACE = ghcr.io/tr-aheiev
IMG_NAME = clustersecret
IMG_FQNAME = $(IMG_NAMESPACE)/$(IMG_NAME)
IMG_VERSION = 2.0.4

.PHONY: container push clean test-build-amd64 test-build-arm64 test-build-all
all: container

# --- Local test builds (use Dockerfile.gh; Docker caches layers so repeats are fast) ---
# Tip: avoid 'docker build --no-cache'; then apt/pip layers reuse cache and only code changes rebuild.
test-build-amd64:
	docker build --platform linux/amd64 -f Dockerfile.gh -t $(IMG_FQNAME):test-amd64 .

test-build-arm64:
	docker build --platform linux/arm64/v8 -f Dockerfile.gh -t $(IMG_FQNAME):test-arm64 .

# Both platforms used in CI (pre-push check)
test-build-all:
	docker build --platform linux/amd64 -f Dockerfile.gh -t $(IMG_FQNAME):test-amd64 .
	docker build --platform linux/arm64/v8 -f Dockerfile.gh -t $(IMG_FQNAME):test-arm64 .

build:
	uname | grep "Darwin" && podman machine start
	podman build -t $(IMG_FQNAME):$(IMG_VERSION) .


container:
	for ARCH in amd64 arm64v8; do \
		sudo docker build -t $(IMG_FQNAME)-$$ARCH:$(IMG_VERSION) -t $(IMG_FQNAME)-$$ARCH:latest --build-arg ARCH=$$ARCH/ .; \
	done

# not push anymore with this. check the github actions
push:
	for ARCH in amd64 arm64v8; do \
		sudo docker push $(IMG_FQNAME)-$$ARCH:latest; \
		sudo docker push $(IMG_FQNAME)-$$ARCH:$(IMG_VERSION); \
	done
	sudo docker manifest create \
		$(IMG_FQNAME):latest \
		--amend $(IMG_FQNAME)-amd64:$(IMG_VERSION) \
		--amend $(IMG_FQNAME)-arm64v8:$(IMG_VERSION)
	sudo docker manifest push $(IMG_FQNAME):latest
	sudo docker manifest create \
		$(IMG_FQNAME):$(IMG_VERSION) \
		--amend $(IMG_FQNAME)-amd64:$(IMG_VERSION) \
		--amend $(IMG_FQNAME)-arm64v8:$(IMG_VERSION)
	sudo docker manifest push $(IMG_FQNAME):$(IMG_VERSION) 

clean:
	for ARCH in amd64 arm64v8; do \
		sudo docker rmi $(IMG_FQNAME)-$$ARCH:latest; \
		sudo docker rmi $(IMG_FQNAME)-$$ARCH:$(IMG_VERSION); \
	done

beta:
	sudo docker build -t $(IMG_FQNAME):$(IMG_VERSION)-beta .
	sudo docker push $(IMG_FQNAME):$(IMG_VERSION)-beta

install:
	helm install clustersecret ./charts/cluster-secret -n clustersecret --create-namespace

start-test-env:
	podman machine start
	KIND_EXPERIMENTAL_PROVIDER=podman kind create cluster

stop-test-env:
	KIND_EXPERIMENTAL_PROVIDER=podman kind delete cluster
	podman machine stop

chart-update:
	# deprecated, see workflows. chart.clustersecret.com from branch gh-pages on /root folder.
	helm package charts/cluster-secret/ -d docs/
	helm repo index ./docs

dev-prepare:
	kubectl apply -f ./yaml/00_rbac.yaml
	kubectl apply -f ./yaml/01_crd.yaml

dev-run: dev-prepare
	kopf run ./src/handlers.py --verbose -A
