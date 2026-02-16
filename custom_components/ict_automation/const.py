DOMAIN = "ict_automation"

CONF_HOST = "host"
CONF_PORT = "port"
CONF_PASSWORD = "password"

CONF_DOORS = "doors"
CONF_AREAS = "areas"
CONF_INPUTS = "inputs"
CONF_OUTPUTS = "outputs"
# Troubles are now part of inputs, but we keep the key just in case of legacy usage
CONF_TROUBLES = "troubles" 

# New Constants for Arming Modes
CONF_ENABLE_AWAY = "enable_arm_away"
CONF_ENABLE_STAY = "enable_arm_stay"
CONF_ENABLE_NIGHT = "enable_arm_night"
CONF_ENABLE_BYPASS = "enable_arm_bypass"

# --- Door Control Commands ---
CMD_DOOR_LOCK = 1             # Locks the door
CMD_DOOR_UNLOCK_LATCH = 2     # Unlocks and holds it open (For the Toggle Switch)
CMD_DOOR_UNLOCK_MOMENTARY = 3 # Unlocks for defined time then relocks (For the Button)

# --- Input Control Commands ---
CMD_INPUT_BYPASS_ONCE = 1      # Bypass until next disarm
CMD_INPUT_UNBYPASS = 2         # Clear bypass (Active)
CMD_INPUT_BYPASS_PERMANENT = 3 # Bypass permanently