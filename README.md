<p align="center">
  <img src="custom_components/ict_automation/icon.png" width="150" height="150" alt="ICT Automation Icon">
</p>

# ICT Protege Automation for Home Assistant

A custom integration to control **ICT Protege WX** and **Protege GX** systems via Home Assistant.

This integration connects directly to the ICT Controller's **Automation Service** (TCP Port 21000), allowing for real-time status updates and control of Doors, Areas, Inputs, and Outputs.

## Features

* **🚪 Doors:**
    * Lock and Unlock controls.
    * Real-time status: Open/Closed and Locked/Unlocked.
* **🛡️ Areas:**
    * Arm (Away) and Disarm controls.
    * Real-time status: Armed/Disarmed and Alarm Active.
* **🔌 Inputs & Troubles:**
    * Monitors physical state (Open/Closed).
    * **Automatic Trouble Detection:** Automatically creates a secondary "Trouble" sensor for every input to monitor tampering, short circuits, or cut wires.
    * **Bypassing:** Includes a dropdown select to Bypass/Unbypass inputs directly from HA.
* **💡 Outputs:**
    * Turn PGMs and other outputs On/Off.

---

## ⚙️ ICT Controller Configuration (Prerequisites)

Before installing, you must configure the **Automation Service** on your ICT Controller. This is found under **System > Services > Automation** (or similar depending on your WX/GX version).

**Match these settings exactly** to ensure the integration can communicate with your controller:

| Setting | Value | Note |
| :--- | :--- | :--- |
| **IP Port** | `21000` | Default automation port |
| **Encryption Level** | `None` | Currently supported mode |
| **Checksum Type** | `8 Bit Sum` | Required for protocol matching |

### Recommended Options (Checkboxes)
Refer to the screenshot below for the tested configuration:

* [ ] **Numbers are Big Endian** (MUST BE UNCHECKED - Critical)
* [x] **Allow Status Requests When Not Logged In**
* [x] **Ack Commands**
* [ ] **Expect Ack For Status Monitoring** (Leave unchecked to prevent connection drops)

> **⚠️ Important:** The "Numbers are Big Endian" box must be **unchecked**. This integration uses Little Endian byte order. If checked, IDs will be interpreted incorrectly.

---

## 📥 Installation

### Method 1: HACS (Recommended)
1.  Open **HACS** in Home Assistant.
2.  Go to **Integrations** > **Custom repositories** (top right menu).
3.  Paste this repository URL and select **Integration**.
4.  Click **Download**.
5.  **Restart Home Assistant**.

### Method 2: Manual
1.  Download the repository as a ZIP file.
2.  Extract the `custom_components/ict_automation` folder.
3.  Copy this folder into your Home Assistant's `/config/custom_components/` directory.
4.  Restart Home Assistant.

---

## 🔧 Configuration

1.  Go to **Settings > Devices & Services > Add Integration**.
2.  Search for **"ICT Protege Automation"**.
3.  Enter your connection details:
    * **Host:** IP Address of your ICT Controller (e.g., `192.168.1.50`).
    * **Port:** `21000`.
    * **Service PIN:** A valid User PIN from your ICT system.

> **🔐 About the Service PIN:**
> The PIN you enter here is used to authenticate the connection. This user **must have permission** to control the Doors, Areas, and Outputs you intend to use. If the user does not have "Door Control" permissions in Protege, Home Assistant will be unable to unlock doors.

---

## 🔎 Finding Device IDs

When adding devices (Doors, Areas, etc.) to the integration, you must enter the correct **ID**.

### Method 1: The Default Way (Recommended)
By default, the integration expects the **Database Record ID** (often labeled as "Reporting ID").
1.  Open Protege WX/GX.
2.  Go to **Programming > [Doors/Areas/Inputs]**.
3.  Look for the column labeled **Record**, **ID**, or **Reporting ID**.
4.  Enter that specific number into Home Assistant.
    * *Example:* If your front door is Record `12`, enter `12` (even if it is the first item in the list).

### Method 2: The "Display Order" Way
**Only use this method** if you have explicitly added the command `ACPUseDisplayOrder = true` to your Controller's command string settings.
1.  Open Protege WX/GX.
2.  Ignore the ID column.
3.  Count the rows from the top of the list (1, 2, 3...).
4.  Enter the **Row Number** into Home Assistant.

---

## 📝 Usage & Troubleshooting

* **"Authentication Failed" Error:**
    * Ensure the PIN is correct.
    * Ensure the user associated with that PIN has valid permissions in Protege.
    * Check that no other system (like a DVR or second integration) is blocking the Automation Port.

* **Inputs show as "Closed" but are actually Open:**
    * Check the **"Numbers are Big Endian"** setting in the ICT Services menu. It must be **OFF**.

* **Status updates are slow:**
    * The integration relies on the controller pushing updates. Ensure "Ack Commands" is enabled in the ICT Service settings so the controller knows we are listening.
