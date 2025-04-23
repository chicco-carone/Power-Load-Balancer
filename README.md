# 🏠⚡ Power Load Balancer

A Home Assistant custom integration to prevent exceeding your household's power budget by automatically turning off less critical appliances based on real-time power consumption.

## ✨ Features

-   Monitors the total house power consumption from a main sensor.
-   Compares current usage against a configured power budget.
-   Automatically turns off pre-defined controllable appliances when the budget is exceeded.
-   Allows configuring multiple monitored circuits/sensors, each linked to a controllable appliance.
-   Prioritize which appliances to turn off based on configured importance levels.
-   Flag certain appliances as "last resort" to only turn them off if absolutely necessary.
-   Optional "assumed power on" value for appliances to react quickly to potential overloads before sensor data updates.
-   Provides a control switch entity to easily enable or disable the load balancing function.
-   Includes a sensor entity to log balancing events (e.g., which appliance was turned off and why).

## ⚠️ Prerequisites

Before installing, ensure you have:

-   Home Assistant (version 202X.X or later is recommended).
-   Power sensors integrated into Home Assistant that provide real-time power consumption (in Watts).
-   Controllable switches or light entities integrated into Home Assistant for the appliances you want to manage.
-   HACS (Home Assistant Community Store) is recommended for easier installation and updates.

## 💾 Installation

### Via HACS (Recommended)

1.  In Home Assistant, navigate to **HACS -> Integrations**.
2.  Click the three dots in the top right corner and select **"Custom repositories"**.
3.  Add the URL of **your GitHub repository** for this integration.
4.  Select the category **"Integration"**.
5.  Click **"Add"**.
6.  Search for "Power Load Balancer" in the HACS Integrations list.
7.  Click on the integration, then click **"Download"**.
8.  Restart Home Assistant.

### Manual Installation

1.  Navigate to your Home Assistant configuration directory (where your `configuration.yaml` is located).
2.  Create a `custom_components` directory if it doesn't exist.
3.  Clone this repository or download the contents and place the `power_load_balancer` folder inside the `custom_components` directory.
    ```bash
    # Example using git clone
    cd /path/to/your_config_dir
    mkdir custom_components
    cd custom_components
    git clone https://github.com/your_github_username/power_load_balancer.git
    ```
4.  Rename the cloned directory to `power_load_balancer` if applicable.
5.  Restart Home Assistant.

## ⚙️ Configuration

After installation and restart:

1.  In Home Assistant, go to **Settings -> Devices & Services**.
2.  Click the orange **"+ ADD INTEGRATION"** button.
3.  Search for "Power Load Balancer".
4.  Follow the steps in the configuration flow:
    *   **Main Configuration:**
        *   Select your **Main Power Sensor** (the sensor representing the total house consumption).
        *   Enter your **Power Budget (Watt)**.
    *   **Add New Monitored Sensor:**
        *   Select a **Power Sensor** for a specific appliance or circuit you want to monitor.
        *   Optionally provide a **Name** for this configuration (defaults to the sensor's friendly name).
        *   Set the **Importance** (1 being highest priority to keep on, 10 being lowest priority).
        *   Check the **Last Resort** box if this appliance should only be turned off as a last resort.
        *   Select the **Controllable Appliance** (switch or light entity) associated with this sensor.
        *   Optionally enter the **Assumed Power On Consumption (Watt)** for this appliance on startup.
    *   You can add multiple monitored sensors and their associated appliances.
    *   You can remove configured sensors from the main configuration screen.
    *   Select **"Finish Configuration"** when done.

## 🕹️ Usage

Once configured, the integration will automatically monitor your power usage and trigger balancing actions when the budget is exceeded and balancing is enabled.

-   **Control Switch:** A switch entity named `switch.power_load_balancer_control_switch` (or similar depending on the automatic entity ID generation) will be created. You can use this switch to manually enable or disable the automatic load balancing function.
-   **Event Log Sensor:** A sensor entity named `sensor.power_load_balancer_event_log` (or similar) will be created. The state will show the last balancing action, and the `events` attribute will contain a list of recent balancing events (e.g., when appliances were turned off).

## ❓ Troubleshooting

-   **Balancing Not Happening:**
    *   Check if the "Power Load Balancer Control" switch is turned **on**.
    *   Verify that your power sensors are reporting data correctly.
    *   Check the Home Assistant logs for any errors related to `power_load_balancer`.
    *   Check the "Power Load Balancer Log" sensor for any error messages or indications of actions taken.
    *   Ensure the configured main power sensor and monitored sensors are reporting in Watts.
-   **Appliance Not Turning Off:**
    *   Verify the correct controllable appliance entity is selected in the configuration.
    *   Check if the appliance was already turned off by the balancer (it won't try to turn it off again).
    *   Ensure the appliance is not marked as "Last Resort" if other, higher importance appliances are still on.

## 🤝 Contributing

Contributions are welcome! If you find a bug or have an idea for a new feature, please open an issue or submit a pull request on the GitHub repository.

## 📄 License

This project is licensed under the [MIT License](LICENSE) - see the LICENSE file for details.

---

**Disclaimer:** This is a custom component and is not officially endorsed or supported by the Home Assistant team. Use at your own risk. Ensure your electrical wiring and appliances are suitable for this type of automation.

