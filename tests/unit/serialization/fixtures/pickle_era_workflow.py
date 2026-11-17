# /// script
# dependencies = []
# 
# [tool.griptape-nodes]
# name = "pickle_era"
# schema_version = "0.20.0"
# engine_version_created_with = "0.102.0"
# node_libraries_referenced = [["Pickle Era Fixture Library", "0.1.0"]]
# node_types_used = [["Pickle Era Fixture Library", "LegacyValuesNode"]]
# is_griptape_provided = false
# is_internal = false
# creation_date = 2024-01-01T00:00:00Z
# last_modified_date = 2026-09-24T17:54:27.367701Z
# 
# ///

import pickle
from enum import EnumType
from griptape.artifacts.image_url_artifact import ImageUrlArtifact
from griptape.rules.ruleset import Ruleset
from griptape_nodes.node_library.library_registry import IconVariant, NodeDeprecationMetadata, NodeMetadata
from griptape_nodes.retained_mode.events.connection_events import CreateConnectionRequest
from griptape_nodes.retained_mode.events.flow_events import CreateFlowRequest
from griptape_nodes.retained_mode.events.library_events import RegisterLibraryFromFileRequest
from griptape_nodes.retained_mode.events.node_events import CreateNodeRequest
from griptape_nodes.retained_mode.events.parameter_events import AddParameterToNodeRequest, AlterParameterDetailsRequest, SetParameterValueRequest
from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes

async def build_workflow() -> None:
    await GriptapeNodes.ahandle_request(RegisterLibraryFromFileRequest(library_name='Pickle Era Fixture Library', perform_discovery_if_not_found=True))
    context_manager = GriptapeNodes.ContextManager()
    if not context_manager.has_current_workflow():
        context_manager.push_workflow(file_path=__file__)
    from griptape_nodes.node_libraries.pickle_era_fixture_library.legacy_values_node import FixtureMode, FixtureUrlArtifact
    # 1. We've collated all of the unique parameter values into a dictionary so that we do not have to duplicate them.
    #    This minimizes the size of the code, especially for large objects like serialized image files.
    # 2. We're using a prefix so that it's clear which Flow these values are associated with.
    # 3. The values are serialized using pickle, which is a binary format. This makes them harder to read, but makes
    #    them consistently save and load. It allows us to serialize complex objects like custom classes, which otherwise
    #    would be difficult to serialize.
    top_level_unique_values_dict = {'2e0d950d-0c16-4858-a19a-c3660ae82dd2': pickle.loads(b'\x80\x04\x95\t\x00\x00\x00\x00\x00\x00\x00\x8c\x05hello\x94.'), 'e6b745b6-4b2f-4707-a782-6516e0f3ef64': pickle.loads(b'\x80\x04K\x07.'), 'd30089bc-5938-447c-9876-6e1320d14ece': pickle.loads(b'\x80\x04\x88.'), '5c8a19c8-2328-40d9-934a-4521d3a49e4a': pickle.loads(b'\x80\x04\x95\n\x00\x00\x00\x00\x00\x00\x00G?\xe0\x00\x00\x00\x00\x00\x00.'), '5ef5ddb2-d1cd-4d9a-850a-3f9580b14e78': pickle.loads(b'\x80\x04\x95\x16\x00\x00\x00\x00\x00\x00\x00]\x94(K\x01\x8c\x03two\x94G@\x08\x00\x00\x00\x00\x00\x00e.'), '506adc7c-f2d5-4b69-8c18-43d8f40d8547': pickle.loads(b'\x80\x04\x95\x15\x00\x00\x00\x00\x00\x00\x00}\x94(\x8c\x01a\x94K\x01\x8c\x01b\x94]\x94(\x88Neu.'), 'd7455757-27d1-454e-9ce4-512a015403ae': pickle.loads(b'\x80\x04\x95\x15\x00\x00\x00\x00\x00\x00\x00}\x94(K\x01\x8c\x03one\x94K\x02\x8c\x03two\x94u.'), '2bf98c5a-2f89-48e3-a0b3-36f99376b194': pickle.loads(b'\x80\x04\x95\t\x00\x00\x00\x00\x00\x00\x00K\x01\x8c\x01b\x94\x86\x94.'), '04ce7f9a-fbf4-432c-ae54-fe2dc3a60c6f': pickle.loads(b'\x80\x04\x95\x07\x00\x00\x00\x00\x00\x00\x00C\x03\x00\x01\xff\x94.'), '7da1ebb8-b6cd-4815-8f4b-b49b990d1251': pickle.loads(b'\x80\x04\x95\xed\x00\x00\x00\x00\x00\x00\x00\x8c%griptape.artifacts.image_url_artifact\x94\x8c\x10ImageUrlArtifact\x94\x93\x94)\x81\x94}\x94(\x8c\x04type\x94h\x01\x8c\x0bmodule_name\x94h\x00\x8c\x02id\x94\x8c\x08image-id\x94\x8c\treference\x94N\x8c\x04meta\x94}\x94\x8c\x04name\x94\x8c\x03cat\x94\x8c\x16encoding_error_handler\x94\x8c\x06strict\x94\x8c\x08encoding\x94\x8c\x05utf-8\x94\x8c\x05value\x94\x8c\x1bhttps://example.com/cat.png\x94ub.'), 'a35ae398-07d5-4fd0-aee0-96d7cec900f1': pickle.loads(b'\x80\x04\x957\x01\x00\x00\x00\x00\x00\x00]\x94(\x8c%griptape.artifacts.image_url_artifact\x94\x8c\x10ImageUrlArtifact\x94\x93\x94)\x81\x94}\x94(\x8c\x04type\x94h\x02\x8c\x0bmodule_name\x94h\x01\x8c\x02id\x94\x8c\x04a-id\x94\x8c\treference\x94N\x8c\x04meta\x94}\x94\x8c\x04name\x94\x8c\x01a\x94\x8c\x16encoding_error_handler\x94\x8c\x06strict\x94\x8c\x08encoding\x94\x8c\x05utf-8\x94\x8c\x05value\x94\x8c\x19https://example.com/a.png\x94ubh\x03)\x81\x94}\x94(h\x06h\x02h\x07h\x01h\x08\x8c\x04b-id\x94h\nNh\x0b}\x94h\r\x8c\x01b\x94h\x0fh\x10h\x11h\x12h\x13\x8c\x19https://example.com/b.png\x94ube.'), '4484a164-9ef6-45f7-abe5-e035e2f1785b': pickle.loads(b'\x80\x04\x95N\x01\x00\x00\x00\x00\x00\x00\x8c\x16griptape.rules.ruleset\x94\x8c\x07Ruleset\x94\x93\x94)\x81\x94}\x94(\x8c\x04type\x94h\x01\x8c\x0bmodule_name\x94h\x00\x8c\x02id\x94\x8c\nruleset-id\x94\x8c\x04name\x94\x8c\x05style\x94\x8c\x0eruleset_driver\x94\x8c-griptape.drivers.ruleset.local_ruleset_driver\x94\x8c\x12LocalRulesetDriver\x94\x93\x94)\x81\x94}\x94(h\x05h\rh\x06h\x0c\x8c\x0fraise_not_found\x94\x88\x8c\x0bpersist_dir\x94Nub\x8c\x04meta\x94}\x94\x8c\x05rules\x94]\x94\x8c\x13griptape.rules.rule\x94\x8c\x04Rule\x94\x93\x94)\x81\x94}\x94(h\x05h\x18h\x06h\x17h\x13}\x94\x8c\x05value\x94\x8c\nBe concise\x94ubaub.'), 'aac7b8e8-b3f7-4406-9433-c61809667907': pickle.loads(b'\x80\x04\x95j\x00\x00\x00\x00\x00\x00\x00\x8cKgriptape_nodes.node_libraries.pickle_era_fixture_library.legacy_values_node\x94\x8c\x0bFixtureMode\x94\x93\x94\x8c\x04slow\x94\x85\x94R\x94.'), 'a2dac1f2-ba23-4532-9d6f-f6df8e51271b': pickle.loads(b'\x80\x04\x95\x19\x01\x00\x00\x00\x00\x00\x00\x8cKgriptape_nodes.node_libraries.pickle_era_fixture_library.legacy_values_node\x94\x8c\x12FixtureUrlArtifact\x94\x93\x94)\x81\x94}\x94(\x8c\x04type\x94h\x01\x8c\x0bmodule_name\x94h\x00\x8c\x02id\x94\x8c\x08model-id\x94\x8c\treference\x94N\x8c\x04meta\x94}\x94\x8c\x04name\x94\x8c\x05model\x94\x8c\x16encoding_error_handler\x94\x8c\x06strict\x94\x8c\x08encoding\x94\x8c\x05utf-8\x94\x8c\x05value\x94\x8c\x1dhttps://example.com/model.glb\x94ub.'), '94a51a2d-215d-461a-9422-ff44e08ed70f': pickle.loads(b'\x80\x04\x95\xf4\x00\x00\x00\x00\x00\x00\x00\x8c%griptape.artifacts.image_url_artifact\x94\x8c\x10ImageUrlArtifact\x94\x93\x94)\x81\x94}\x94(\x8c\x04type\x94h\x01\x8c\x0bmodule_name\x94h\x00\x8c\x02id\x94\x8c\tresult-id\x94\x8c\treference\x94N\x8c\x04meta\x94}\x94\x8c\x04name\x94\x8c\x06result\x94\x8c\x16encoding_error_handler\x94\x8c\x06strict\x94\x8c\x08encoding\x94\x8c\x05utf-8\x94\x8c\x05value\x94\x8c\x1ehttps://example.com/result.png\x94ub.')}
    # Create the Flow, then do work within it as context.
    flow0_name = (await GriptapeNodes.ahandle_request(CreateFlowRequest(parent_flow_name=None, flow_name='ControlFlow_1', set_as_new_context=False, metadata={}))).flow_name
    with GriptapeNodes.ContextManager().flow(flow0_name):
        node0_name = (await GriptapeNodes.ahandle_request(CreateNodeRequest(node_type='LegacyValuesNode', specific_library_name='Pickle Era Fixture Library', node_name='Holder', metadata={'library_node_metadata': {'category': 'test', 'description': 'Holds one any-typed parameter per fixture value', 'display_name': 'Legacy Values', 'tags': None, 'icon': None, 'color': None, 'group': None, 'deprecation': None, 'is_node_group': None, 'declarations': []}, 'library': 'Pickle Era Fixture Library', 'node_type': 'LegacyValuesNode'}, initial_setup=True))).node_name
        with GriptapeNodes.ContextManager().node(node0_name):
            await GriptapeNodes.ahandle_request(SetParameterValueRequest(parameter_name='text', node_name=node0_name, value=top_level_unique_values_dict['2e0d950d-0c16-4858-a19a-c3660ae82dd2'], initial_setup=True, is_output=False))
            await GriptapeNodes.ahandle_request(SetParameterValueRequest(parameter_name='count', node_name=node0_name, value=top_level_unique_values_dict['e6b745b6-4b2f-4707-a782-6516e0f3ef64'], initial_setup=True, is_output=False))
            await GriptapeNodes.ahandle_request(SetParameterValueRequest(parameter_name='flag', node_name=node0_name, value=top_level_unique_values_dict['d30089bc-5938-447c-9876-6e1320d14ece'], initial_setup=True, is_output=False))
            await GriptapeNodes.ahandle_request(SetParameterValueRequest(parameter_name='ratio', node_name=node0_name, value=top_level_unique_values_dict['5c8a19c8-2328-40d9-934a-4521d3a49e4a'], initial_setup=True, is_output=False))
            await GriptapeNodes.ahandle_request(SetParameterValueRequest(parameter_name='items', node_name=node0_name, value=top_level_unique_values_dict['5ef5ddb2-d1cd-4d9a-850a-3f9580b14e78'], initial_setup=True, is_output=False))
            await GriptapeNodes.ahandle_request(SetParameterValueRequest(parameter_name='mapping', node_name=node0_name, value=top_level_unique_values_dict['506adc7c-f2d5-4b69-8c18-43d8f40d8547'], initial_setup=True, is_output=False))
            await GriptapeNodes.ahandle_request(SetParameterValueRequest(parameter_name='int_keyed', node_name=node0_name, value=top_level_unique_values_dict['d7455757-27d1-454e-9ce4-512a015403ae'], initial_setup=True, is_output=False))
            await GriptapeNodes.ahandle_request(SetParameterValueRequest(parameter_name='pair', node_name=node0_name, value=top_level_unique_values_dict['2bf98c5a-2f89-48e3-a0b3-36f99376b194'], initial_setup=True, is_output=False))
            await GriptapeNodes.ahandle_request(SetParameterValueRequest(parameter_name='blob', node_name=node0_name, value=top_level_unique_values_dict['04ce7f9a-fbf4-432c-ae54-fe2dc3a60c6f'], initial_setup=True, is_output=False))
            await GriptapeNodes.ahandle_request(SetParameterValueRequest(parameter_name='image', node_name=node0_name, value=top_level_unique_values_dict['7da1ebb8-b6cd-4815-8f4b-b49b990d1251'], initial_setup=True, is_output=False))
            await GriptapeNodes.ahandle_request(SetParameterValueRequest(parameter_name='images', node_name=node0_name, value=top_level_unique_values_dict['a35ae398-07d5-4fd0-aee0-96d7cec900f1'], initial_setup=True, is_output=False))
            await GriptapeNodes.ahandle_request(SetParameterValueRequest(parameter_name='ruleset', node_name=node0_name, value=top_level_unique_values_dict['4484a164-9ef6-45f7-abe5-e035e2f1785b'], initial_setup=True, is_output=False))
            await GriptapeNodes.ahandle_request(SetParameterValueRequest(parameter_name='mode', node_name=node0_name, value=top_level_unique_values_dict['aac7b8e8-b3f7-4406-9433-c61809667907'], initial_setup=True, is_output=False))
            await GriptapeNodes.ahandle_request(SetParameterValueRequest(parameter_name='custom_artifact', node_name=node0_name, value=top_level_unique_values_dict['a2dac1f2-ba23-4532-9d6f-f6df8e51271b'], initial_setup=True, is_output=False))
            await GriptapeNodes.ahandle_request(SetParameterValueRequest(parameter_name='result', node_name=node0_name, value=top_level_unique_values_dict['94a51a2d-215d-461a-9422-ff44e08ed70f'], initial_setup=True, is_output=True))
