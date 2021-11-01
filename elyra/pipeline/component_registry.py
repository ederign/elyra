#
# Copyright 2018-2021 Elyra Authors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
import json
import os
import time
from types import SimpleNamespace
from typing import Dict
from typing import List
from typing import Optional

from jinja2 import Environment
from jinja2 import PackageLoader
from jinja2 import Template
from traitlets import default
from traitlets import Integer
from traitlets.config import LoggingConfigurable  # noqa: H306 (alphabetical order catch-22)

from elyra.metadata.manager import MetadataManager
from elyra.metadata.metadata import Metadata
from elyra.metadata.schemaspaces import ComponentRegistries
from elyra.pipeline.component import Component
from elyra.pipeline.component import ComponentParser
from elyra.pipeline.component import ComponentReader
from elyra.pipeline.component import DirectoryComponentReader
from elyra.pipeline.component import FilesystemComponentReader
from elyra.pipeline.component import UrlComponentReader


class ComponentRegistry(LoggingConfigurable):
    """
    Component Registry, responsible to provide a list of available components
    for each runtime. The registry uses component parser to read and parse each
    component entry from the catalog and transform them into a component value object.
    """
    _cached_components: Dict[str, Component] = {}
    _cache_last_updated = None

    _generic_category_label = "Elyra"
    _generic_components: Dict[str, Component] = {
        "notebook": Component(id="notebook",
                              name="Notebook",
                              description="Run notebook file",
                              op="execute-notebook-node",
                              location_type="elyra",
                              location="elyra",
                              extensions=[".ipynb"],
                              categories=[_generic_category_label]),
        "python-script": Component(id="python-script",
                                   name="Python Script",
                                   description="Run Python script",
                                   op="execute-python-node",
                                   location_type="elyra",
                                   location="elyra",
                                   extensions=[".py"],
                                   categories=[_generic_category_label]),
        "r-script": Component(id="r-script",
                              name="R Script",
                              description="Run R script",
                              op="execute-r-node",
                              location_type="elyra",
                              location="elyra",
                              extensions=[".r"],
                              categories=[_generic_category_label])}

    ttl_default = 300
    cache_ttl_env = 'ELYRA_COMPONENT_REGISTRY_CACHE_TTL'
    cache_ttl = Integer(ttl_default,
                        help="Time-to-live (in seconds) for Component Registry cache entries. "
                             "(ELYRA_COMPONENT_REGISTRY_CACHE_TTL env var)").tag(config=True)

    @default('cache_ttl')
    def cache_ttl_default(self):
        ttl = ComponentRegistry.ttl_default
        try:
            ttl = int(os.getenv(self.cache_ttl_env, ttl))
        except ValueError:
            pass
        return ttl

    def __init__(self,
                 parser: ComponentParser,
                 caching_enabled: bool = True,
                 **kwargs):
        super().__init__(**kwargs)
        self._parser = parser

        # Initialize the cache
        self.caching_enabled = caching_enabled
        if self.caching_enabled:
            self.log.debug(f"ComponentRegistry cache TTL: {self.cache_ttl}")
            self.update_cache()

    def get_all_components(self) -> List[Component]:
        """
        Retrieve all components; use the component registry cache if enabled
        """
        if self.caching_enabled:
            if self._is_cache_expired():
                self.update_cache()
            return list(self._cached_components.values())

        return list(self._read_component_registries().values())

    def get_component(self, component_id: str) -> Optional[Component]:
        """
        Retrieve the component with a given component_id; use component registry
        cache if enabled
        """
        component: Component
        if self.caching_enabled:
            if self._is_cache_expired():
                self.update_cache()
            component = self._cached_components.get(component_id)
        else:
            component = self._read_component_registries().get(component_id)

        if component is None:
            self.log.error(f"Component with ID '{component_id}' could not be found in any "
                           f"{self._parser.component_platform} registries.")

        return component

    def update_cache(self, registry: Optional[Metadata] = None, operation: Optional[str] = None):
        updated_components = self._read_component_registries([registry] if registry else None)

        if operation == 'modify':
            # Replace only the components most recently re-read
            for component_id, component in updated_components.items():
                self._cached_components[component_id] = component
        elif operation == 'delete':
            # Remove only the components most recently re-read
            for component_id, component in updated_components.items():
                self._cached_components.pop(component_id)
        else:
            # Replace all components in the cache
            self._cached_components = updated_components

        self._cache_last_updated = time.time()

    def _is_cache_expired(self) -> bool:
        is_expired = True
        if self._cache_last_updated:
            now = time.time()
            elapsed = int(now - self._cache_last_updated)
            if elapsed < self.cache_ttl:
                is_expired = False

        return is_expired

    @staticmethod
    def get_generic_components() -> List[Component]:
        return list(ComponentRegistry._generic_components.values())

    @staticmethod
    def get_generic_component(component_id: str) -> Component:
        return ComponentRegistry._generic_components.get(component_id)

    @staticmethod
    def load_jinja_template(template_name: str) -> Template:
        """
        Loads the jinja template of the given name from the
        elyra/templates/components folder
        """
        loader = PackageLoader('elyra', 'templates/components')
        template_env = Environment(loader=loader)

        return template_env.get_template(template_name)

    @staticmethod
    def to_canvas_palette(components: List[Component]) -> Dict:
        """
        Converts registry components into appropriate canvas palette format
        """
        template = ComponentRegistry.load_jinja_template('canvas_palette_template.jinja2')

        # Define a fallback category for components with no given categories
        fallback_category_name = "No Category"

        # Convert the list of all components into a dictionary of
        # component lists keyed by category
        category_dict: Dict[str, List[Component]] = {}
        for component in components:
            categories = component.categories

            # Assign a fallback category so that component is not
            # lost during palette render
            if not categories:
                categories = [fallback_category_name]

            for category in categories:
                if category not in category_dict.keys():
                    category_dict[category] = []

                if component.id not in [comp.id for comp in category_dict[category]]:
                    category_dict[category].append(component)

        # Render template
        canvas_palette = template.render(category_dict=category_dict)
        return json.loads(canvas_palette)

    @staticmethod
    def to_canvas_properties(component: Component) -> Dict:
        """
        Converts registry components into appropriate canvas properties format

        If component_id is one of the generic set, generic template is rendered,
        otherwise, the  runtime-specific property template is rendered
        """
        if component.id in ('notebook', 'python-script', 'r-script'):
            template = ComponentRegistry.load_jinja_template('generic_properties_template.jinja2')
        else:
            template = ComponentRegistry.load_jinja_template('canvas_properties_template.jinja2')

        canvas_properties = template.render(component=component)
        return json.loads(canvas_properties)

    def _read_component_registries(self, registries: Optional[List[Metadata]] = None) -> Dict[str, Component]:
        """
        Read through component registries and return a dictionary of components indexed by component_id.

        :param registries: a list of metadata instances from which to read and construct Component objects;
                           if none provided, all registries for the active runtime platform are assumed

        :returns: a dictionary of component id to Component object for all read/parsed components
        """
        component_dict: Dict[str, Component] = {}

        if not registries:
            registries = self._get_registries_for_runtime()
        for registry in registries:
            self.log.debug(f"Component registry: processing components in registry '{registry.display_name}'")

            registry_categories = registry.metadata.get("categories", [])
            registry_location_type = registry.metadata['location_type'].lower()

            # Assign reader based on the location type of the registry (file, directory, url)
            reader = self._get_reader(registry_location_type, self._parser.file_types)

            # Get content of component definition file for each component in this registry
            component_definitions = reader.read_component_definitions(registry.metadata['paths'])
            for path, component_definition in component_definitions.items():

                component_entry = {
                    "location_type": reader.resource_type,
                    "location": path,
                    "categories": registry_categories,
                    "component_definition": component_definition
                }

                # Parse the component entry to get a fully qualified Component object
                components = self._parser.parse(SimpleNamespace(**component_entry)) or []
                for component in components:
                    component_dict[component.id] = component

        return component_dict

    def _get_registries_for_runtime(self) -> List[Metadata]:
        """
        Retrieve the registries relevant to the calling processor instance
        """
        runtime_registries = []
        try:
            registries = MetadataManager(schemaspace=ComponentRegistries.COMPONENT_REGISTRIES_SCHEMASPACE_ID)\
                .get_all()

            # Filter registries according to processor type
            runtime_registries = [r for r in registries if r.metadata['runtime'] == self._parser.component_platform]
        except Exception:
            self.log.error(f"Could not access registries for processor: {self._parser.component_platform}")

        return runtime_registries

    def _get_reader(self, registry_location_type: str, file_types: List[str]) -> ComponentReader:
        """
        Find the proper reader based on the given registry location type
        """
        readers = {
            FilesystemComponentReader.location_type: FilesystemComponentReader(file_types),
            DirectoryComponentReader.location_type: DirectoryComponentReader(file_types),
            UrlComponentReader.location_type: UrlComponentReader(file_types)
        }

        reader = readers.get(registry_location_type)
        if not reader:
            raise ValueError(f"Unsupported registry type: '{registry_location_type}'")

        return reader
