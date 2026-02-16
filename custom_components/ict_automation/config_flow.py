import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers import selector, entity_registry as er
from .const import DOMAIN, CONF_HOST, CONF_PORT, CONF_PASSWORD, CONF_DOORS, CONF_AREAS, CONF_INPUTS, CONF_OUTPUTS, CONF_TROUBLES
from .ict_library import ICTClient
import logging
import yaml
import asyncio

_LOGGER = logging.getLogger(__name__)

class ICTConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1
    async def async_step_user(self, user_input=None):
        if user_input is not None:
            return self.async_create_entry(title=f"ICT ({user_input[CONF_HOST]})", data=user_input)
        return self.async_show_form(step_id="user", data_schema=vol.Schema({
            vol.Required(CONF_HOST): str,
            vol.Required(CONF_PORT, default=21000): int,
            vol.Required(CONF_PASSWORD): str,
        }))

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return ICTOptionsFlowHandler(config_entry)

class ICTOptionsFlowHandler(config_entries.OptionsFlow):
    def __init__(self, config_entry):
        self._config_entry = config_entry 
        self.options = dict(config_entry.options)
        self.data = dict(config_entry.data)
        
        # Ensure dicts exist
        self.options.setdefault(CONF_DOORS, {})
        self.options.setdefault(CONF_AREAS, {})
        self.options.setdefault(CONF_INPUTS, {})
        self.options.setdefault(CONF_TROUBLES, {})
        self.options.setdefault(CONF_OUTPUTS, {})
        
        self._edit_type = None
        self._edit_id = None

    def _get_dict(self, key):
        data = self.options.get(key, {})
        if isinstance(data, dict): return {int(k): v for k, v in data.items()}
        return {}

    def _save_options(self):
        self.hass.config_entries.async_update_entry(self._config_entry, options=self.options)

    async def async_step_init(self, user_input=None):
        return self.async_show_menu(step_id="init", menu_options=[
            "scan_devices", "raw_editor", "configure_connection",
            "add_door", "add_area", "add_input", "add_trouble", "add_output", 
            "edit_device", "remove_device"
        ])

    # --- RAW YAML EDITOR ---
    async def async_step_raw_editor(self, user_input=None):
        errors = {}
        if user_input is not None:
            try:
                raw_data = yaml.safe_load(user_input["config_yaml"])
                if not isinstance(raw_data, dict): raise ValueError("Root must be a dictionary")
                self.options[CONF_DOORS] = self._parse_raw_section(raw_data.get("doors", {}))
                self.options[CONF_AREAS] = self._parse_raw_section(raw_data.get("areas", {}))
                self.options[CONF_INPUTS] = self._parse_raw_section(raw_data.get("inputs", {}))
                self.options[CONF_TROUBLES] = self._parse_raw_section(raw_data.get("troubles", {}))
                self.options[CONF_OUTPUTS] = self._parse_raw_section(raw_data.get("outputs", {}))
                self._save_options()
                return self.async_create_entry(title="", data=self.options)
            except Exception: errors["base"] = "yaml_error"

        current_config = {
            "doors": self._get_dict(CONF_DOORS), "areas": self._get_dict(CONF_AREAS),
            "inputs": self._get_dict(CONF_INPUTS), "troubles": self._get_dict(CONF_TROUBLES),
            "outputs": self._get_dict(CONF_OUTPUTS)
        }
        yaml_str = yaml.dump(current_config, sort_keys=True, allow_unicode=True)
        return self.async_show_form(step_id="raw_editor", data_schema=vol.Schema({vol.Required("config_yaml", default=yaml_str): selector.TextSelector(selector.TextSelectorConfig(multiline=True))}), errors=errors)

    def _parse_raw_section(self, section):
        if not section: return {}
        return {int(k): str(v) for k, v in section.items()}

    # --- ADD ITEMS (WIZARD STYLE) ---
    async def _add_item_step(self, user_input, type_name, storage_key, step_id):
        storage_dict = self._get_dict(storage_key)
        errors = {}
        
        if user_input is not None:
            dev_id = int(user_input["dev_id"])
            if dev_id in storage_dict: 
                errors["base"] = "id_exists"
            else:
                # 1. Update Memory
                storage_dict[dev_id] = user_input["name"]
                self.options[storage_key] = storage_dict
                
                # 2. Check which button was pressed
                if user_input.get("next_action") == "add_more":
                    return self.async_show_form(
                        step_id=step_id, 
                        data_schema=self._get_schema_wizard(), 
                        description_placeholders={"type": type_name}
                    )
                else:
                    # "Save & Finish" -> Write to disk and Reload
                    self._save_options()
                    return self.async_create_entry(title="", data=self.options)
                    
        return self.async_show_form(
            step_id=step_id, 
            data_schema=self._get_schema_wizard(), 
            errors=errors, 
            description_placeholders={"type": type_name}
        )

    def _get_schema_wizard(self):
        return vol.Schema({
            vol.Required("dev_id"): int, 
            vol.Required("name"): str, 
            vol.Required("next_action", default="add_more"): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=[
                        {"value": "add_more", "label": "Save & Add Another"},
                        {"value": "finish", "label": "Save & Finish"}
                    ],
                    mode=selector.SelectSelectorMode.LIST,
                    translation_key="next_action"
                )
            )
        })

    async def async_step_add_door(self, user_input=None): return await self._add_item_step(user_input, "door", CONF_DOORS, "add_door")
    async def async_step_add_area(self, user_input=None): return await self._add_item_step(user_input, "area", CONF_AREAS, "add_area")
    async def async_step_add_input(self, user_input=None): return await self._add_item_step(user_input, "input", CONF_INPUTS, "add_input")
    async def async_step_add_trouble(self, user_input=None): return await self._add_item_step(user_input, "trouble", CONF_TROUBLES, "add_trouble")
    async def async_step_add_output(self, user_input=None): return await self._add_item_step(user_input, "output", CONF_OUTPUTS, "add_output")

    # --- REMOVE ITEMS (FIXED) ---
    async def async_step_remove_device(self, user_input=None):
        return self.async_show_menu(step_id="remove_device", menu_options=["remove_door", "remove_area", "remove_input", "remove_trouble", "remove_output", "back"])

    async def _remove_step(self, user_input, storage_key, step_id):
        storage_dict = self._get_dict(storage_key)
        
        if user_input:
            ent_reg = er.async_get(self.hass)
            
            # Map storage key to prefixes
            prefix_map = {
                CONF_DOORS: "ict_door",
                CONF_AREAS: "ict_area",
                CONF_INPUTS: "ict_input",
                CONF_TROUBLES: "ict_trouble",
                CONF_OUTPUTS: "ict_output"
            }
            prefix = prefix_map.get(storage_key, "")

            for i in user_input["items"]: 
                try: 
                    dev_id = int(i)
                    # Remove from config
                    if dev_id in storage_dict: 
                        del storage_dict[dev_id]
                    
                    # Remove entities from registry
                    if prefix:
                        base_uid = f"{prefix}_{dev_id}"
                        suffixes = ["", "_contact", "_bypass"]
                        
                        # Find and remove all related entities
                        for entry in list(ent_reg.entities.values()):
                            if entry.config_entry_id == self._config_entry.entry_id:
                                for s in suffixes:
                                    if entry.unique_id == f"{base_uid}{s}":
                                        ent_reg.async_remove(entry.entity_id)
                except Exception as e:
                    _LOGGER.error(f"Error removing device {i}: {e}")
                    continue
            
            self.options[storage_key] = storage_dict
            self._save_options()
            return self.async_create_entry(title="", data=self.options)

        if not storage_dict: return self.async_abort(reason="no_devices")
        
        options_list = [selector.SelectOptionDict(value=str(k), label=f"{k}: {v}") for k, v in storage_dict.items()]
        schema = vol.Schema({
            vol.Required("items"): selector.SelectSelector(
                selector.SelectSelectorConfig(options=options_list, mode=selector.SelectSelectorMode.DROPDOWN, multiple=True)
            )
        })
        return self.async_show_form(step_id=step_id, data_schema=schema)

    async def async_step_remove_door(self, user_input=None): return await self._remove_step(user_input, CONF_DOORS, "remove_door")
    async def async_step_remove_area(self, user_input=None): return await self._remove_step(user_input, CONF_AREAS, "remove_area")
    async def async_step_remove_input(self, user_input=None): return await self._remove_step(user_input, CONF_INPUTS, "remove_input")
    async def async_step_remove_trouble(self, user_input=None): return await self._remove_step(user_input, CONF_TROUBLES, "remove_trouble")
    async def async_step_remove_output(self, user_input=None): return await self._remove_step(user_input, CONF_OUTPUTS, "remove_output")

    # --- EDIT ---
    async def async_step_edit_device(self, user_input=None):
        return self.async_show_menu(step_id="edit_device", menu_options=["edit_door", "edit_area", "edit_input", "edit_trouble", "edit_output", "back"])

    async def _edit_select_step(self, user_input, storage_key, step_id):
        storage_dict = self._get_dict(storage_key)
        if user_input:
            self._edit_id = int(user_input["item"])
            self._edit_type = storage_key
            return await self.async_step_edit_confirm()
        if not storage_dict: return self.async_abort(reason="no_devices")
        options_list = [selector.SelectOptionDict(value=str(k), label=f"{k}: {v}") for k, v in storage_dict.items()]
        return self.async_show_form(step_id=step_id, data_schema=vol.Schema({vol.Required("item"): selector.SelectSelector(selector.SelectSelectorConfig(options=options_list, mode=selector.SelectSelectorMode.DROPDOWN))}))

    async def async_step_edit_confirm(self, user_input=None):
        storage = self._get_dict(self._edit_type)
        if user_input:
            storage[self._edit_id] = user_input["name"]
            self.options[self._edit_type] = storage
            self._save_options()
            return self.async_create_entry(title="", data=self.options)
        return self.async_show_form(step_id="edit_confirm", data_schema=vol.Schema({vol.Required("name", default=storage.get(self._edit_id, "")): str}), description_placeholders={"id": str(self._edit_id)})

    async def async_step_edit_door(self, user_input=None): return await self._edit_select_step(user_input, CONF_DOORS, "edit_door")
    async def async_step_edit_area(self, user_input=None): return await self._edit_select_step(user_input, CONF_AREAS, "edit_area")
    async def async_step_edit_input(self, user_input=None): return await self._edit_select_step(user_input, CONF_INPUTS, "edit_input")
    async def async_step_edit_trouble(self, user_input=None): return await self._edit_select_step(user_input, CONF_TROUBLES, "edit_trouble")
    async def async_step_edit_output(self, user_input=None): return await self._edit_select_step(user_input, CONF_OUTPUTS, "edit_output")

    # --- SCANNER (UNCHANGED) ---
    async def async_step_scan_devices(self, user_input=None):
        return self.async_show_menu(step_id="scan_devices", menu_options=["scan_all", "scan_doors", "scan_areas", "scan_inputs", "scan_troubles", "scan_outputs", "back"])

    async def async_step_scan_all(self, user_input=None):
        if user_input: return await self._execute_scan_logic(user_input["limit_areas"], user_input["limit_doors"], user_input["limit_outputs"], user_input["limit_inputs"], user_input["limit_troubles"])
        return self.async_show_form(step_id="scan_all", data_schema=vol.Schema({
            vol.Required("limit_areas", default=10): int, vol.Required("limit_doors", default=20): int,
            vol.Required("limit_outputs", default=20): int, vol.Required("limit_inputs", default=100): int,
            vol.Required("limit_troubles", default=20): int,
        }))

    async def async_step_scan_doors(self, user_input=None):
        if user_input: return await self._execute_scan_logic(limit_doors=user_input["limit"])
        return self.async_show_form(step_id="scan_doors", data_schema=vol.Schema({vol.Required("limit", default=20): int}))
    async def async_step_scan_areas(self, user_input=None):
        if user_input: return await self._execute_scan_logic(limit_areas=user_input["limit"])
        return self.async_show_form(step_id="scan_areas", data_schema=vol.Schema({vol.Required("limit", default=10): int}))
    async def async_step_scan_inputs(self, user_input=None):
        if user_input: return await self._execute_scan_logic(limit_inputs=user_input["limit"])
        return self.async_show_form(step_id="scan_inputs", data_schema=vol.Schema({vol.Required("limit", default=100): int}))
    async def async_step_scan_troubles(self, user_input=None):
        if user_input: return await self._execute_scan_logic(limit_troubles=user_input["limit"])
        return self.async_show_form(step_id="scan_troubles", data_schema=vol.Schema({vol.Required("limit", default=20): int}))
    async def async_step_scan_outputs(self, user_input=None):
        if user_input: return await self._execute_scan_logic(limit_outputs=user_input["limit"])
        return self.async_show_form(step_id="scan_outputs", data_schema=vol.Schema({vol.Required("limit", default=20): int}))

    async def _execute_scan_logic(self, limit_doors=0, limit_areas=0, limit_inputs=0, limit_outputs=0, limit_troubles=0):
        client = None
        is_temp = False
        
        if DOMAIN in self.hass.data and self._config_entry.entry_id in self.hass.data[DOMAIN]:
            client = self.hass.data[DOMAIN][self._config_entry.entry_id]
        
        if not client:
            client = ICTClient(self.data[CONF_HOST], self.data[CONF_PORT], self.data[CONF_PASSWORD])
            if not await client.start_temp_connection(): 
                return self.async_abort(reason="cannot_connect")
            is_temp = True

        if not await client.authenticate():
            if is_temp: await client.stop()
            return self.async_abort(reason="invalid_auth")

        if limit_areas > 0: await self._run_scan(client, 2, limit_areas, self._get_dict(CONF_AREAS), "Area", CONF_AREAS)
        if limit_doors > 0: await self._run_scan(client, 1, limit_doors, self._get_dict(CONF_DOORS), "Door", CONF_DOORS)
        if limit_outputs > 0: await self._run_scan(client, 3, limit_outputs, self._get_dict(CONF_OUTPUTS), "Output", CONF_OUTPUTS)
        if limit_inputs > 0: await self._run_scan(client, 4, limit_inputs, self._get_dict(CONF_INPUTS), "Input", CONF_INPUTS)
        if limit_troubles > 0: await self._run_scan(client, 6, limit_troubles, self._get_dict(CONF_TROUBLES), "Trouble", CONF_TROUBLES)

        if is_temp: await client.stop()
        self._save_options()
        return self.async_create_entry(title="", data=self.options)

    async def _run_scan(self, client, group, limit, storage, name_prefix, conf_key):
        consecutive_fails = 0
        for i in range(1, limit + 1):
            if i in storage: 
                consecutive_fails = 0
                continue
            
            exists = await client.check_exists(group, i)
            await asyncio.sleep(0.1)
            
            if exists:
                storage[i] = f"{name_prefix} {i}"
                consecutive_fails = 0
            else:
                consecutive_fails += 1
                if consecutive_fails >= 5: break
        
        self.options[conf_key] = storage

    async def async_step_configure_connection(self, user_input=None):
        if user_input is not None:
            self.hass.config_entries.async_update_entry(self._config_entry, data=user_input)
            return self.async_create_entry(title="", data=self.options)
        schema = vol.Schema({
            vol.Required(CONF_HOST, default=self.data.get(CONF_HOST)): str,
            vol.Required(CONF_PORT, default=self.data.get(CONF_PORT)): int,
            vol.Required(CONF_PASSWORD, default=self.data.get(CONF_PASSWORD)): str,
        })
        return self.async_show_form(step_id="configure_connection", data_schema=schema)

    async def async_step_back(self, user_input=None): return await self.async_step_init()
