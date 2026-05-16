# Dealmill — defer to the central builder at ~/work/projects/builder/.
#
# BUILDER_PATH resolution:
#   /usr/src/builder  -> inside the dmill1 dev container (the central
#                        `buildsh` recipe copies the builder repo in)
#   /usr/builder      -> alias symlink inside the dev container
#   $(HOME)/work/...  -> on the host (`make help` etc.)
#
# Standard targets (deps / build / test / clean) come from $(BUILDER_PATH)/Makefile.python
# because pyproject.toml is present. The `run` target is overridden in Makefile.local
# so it accepts ARGS="...".

ifneq ($(wildcard /usr/src/builder),)
  BUILDER_PATH ?= /usr/src/builder
else ifneq ($(wildcard /usr/builder),)
  BUILDER_PATH ?= /usr/builder
else
  BUILDER_PATH ?= $(HOME)/work/projects/builder
endif

include $(BUILDER_PATH)/Makefile
