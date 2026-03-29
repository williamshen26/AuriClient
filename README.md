# Hey Auri Client (Home Assistant)

Hey Auri Client is a Home Assistant custom integration that connects your smart home to the Auri voice assistant platform. It lets you interact with an LLM and securely perform actions on your devices from natural language commands—like having a personal smart home butler.

## Features
- LLM-powered conversations for your Home Assistant instance
- Secure, account-based access using `client_id` and `client_secret`
- Works with Home Assistant’s Conversation and Voice Assistant flows

## Prerequisites
- Auri account at https://hey-auri.com
- Home Assistant with HACS installed

## Installation (via HACS)
1. **Create your Auri account**
   - Sign up at https://hey-auri.com
   - Save your `client_id` and `client_secret` in a secure place.

2. **Install HACS (Home Assistant Community Store)**
   - Follow the official HACS installation guide: https://hacs.xyz/docs/setup/download/

3. **Add Hey Auri Client in HACS**
   - In Home Assistant, open **HACS**.
   - Go to **Integrations**.
   - Search for **Hey Auri Client** and click **Download**.
   - Restart Home Assistant when prompted.

4. **Add the integration in Home Assistant**
   - Navigate to **Settings → Devices & Services**.
   - Click **Add Integration**.
   - Search for **Hey Auri Client** and select it.
   - Enter your `client_id` and `client_secret` when prompted.

## Configure a Voice Assistant
1. Go to **Settings → Voice Assistants**.
2. Create a new assistant (or edit an existing one).
3. Set the **Conversation Agent** to **Hey Auri Client**.
4. Save your changes.

## Usage
- Start a conversation in Home Assistant using your new voice assistant.
- The Auri Client will respond and can act on your devices based on your commands.

## Troubleshooting
- Double-check your `client_id` and `client_secret` if authentication fails.
- Ensure Home Assistant has been restarted after installing the integration.
- Verify the integration appears under **Settings → Devices & Services**.

## Support
- Website: https://hey-auri.com

## License
- See the repository license file, if available.
