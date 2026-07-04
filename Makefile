# Package forge as a distributable .skill archive (zip with forge/ root folder).
#
# Layout inside forge.skill:
#   forge/SKILL.md
#   forge/forge.py
#   forge/schema.sql

SKILL_NAME := forge
SRC_DIR    := src
BUILD_DIR  := build
STAGE_DIR  := $(BUILD_DIR)/$(SKILL_NAME)
OUTPUT     := $(SKILL_NAME).skill

SKILL_SOURCES := $(SRC_DIR)/skill.md $(SRC_DIR)/forge.py $(SRC_DIR)/schema.sql

.PHONY: all package clean test check

all: package

check:
	@test -f $(SRC_DIR)/skill.md || (echo "missing $(SRC_DIR)/skill.md" && exit 1)
	@grep -q '^name: $(SKILL_NAME)$$' $(SRC_DIR)/skill.md || \
		(echo "$(SRC_DIR)/skill.md frontmatter name must be '$(SKILL_NAME)'" && exit 1)

$(STAGE_DIR): check $(SKILL_SOURCES)
	@rm -rf "$(STAGE_DIR)"
	@mkdir -p "$(STAGE_DIR)"
	cp $(SRC_DIR)/skill.md "$(STAGE_DIR)/SKILL.md"
	cp $(SRC_DIR)/forge.py $(SRC_DIR)/schema.sql "$(STAGE_DIR)/"

package: $(OUTPUT)

$(OUTPUT): $(STAGE_DIR)
	rm -f "$(OUTPUT)"
	cd "$(BUILD_DIR)" && zip -r -X "../$(OUTPUT)" "$(SKILL_NAME)"
	@echo "built $(OUTPUT)"

test:
	PYTHONPATH=$(SRC_DIR) python3 -m unittest discover -s test -v

clean:
	rm -rf "$(BUILD_DIR)" "$(OUTPUT)"
