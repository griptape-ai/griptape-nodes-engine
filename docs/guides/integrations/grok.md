# How to get and use an xAI Grok API key

Grok is a family of Large Language Models (LLMs) developed by xAI. You can access these models via the GrokPrompt config node. To make use of these, however, you'll need to have an XAI account, and generate an API key. It's worth noting as well, that xAI is a paid service, and to make use of it, you'll need to set up billing details on their website.

## Account and API Key Creation

Before you can get API keys for your xAI account, you'll _need_ a xAI account. To begin, head to [https://x.ai](https://x.ai)

<p align="center">
    <img src="../assets/grok_00_main_page.png" alt="Grok AI" width="500"/>
</p>

!!! info

    If you already have an account, go ahead and skip ahead to [Step 2](#2-set-up-billing)

### 1. Create a xAI account

1. Click on the console login option, or navigate to [https://accounts.x.ai/sign-up](https://accounts.x.ai/sign-up)

    <p align="center">
    <img src="../assets/grok_01_console_login.png" alt="Console Login" width="500"/>
    </p>

1. Complete the signup process, it is extremely straightforward.

    <p align="center">
    <img src="../assets/grok_02_signup.png" alt="Signup" width="500"/>
    </p>

### 2. Set Up Billing

!!! warning "Billing Required"

    Before using xAI models with Griptape Nodes, be aware that xAI _requires_ billing information to be set up. Without this step, the models won't be available for use in your Nodes.

    If you attempt to run Nodes using xAI without completing this billing setup, your workflows will fail as the service will reject the account credentials.

<p align="center">
    <img src="../assets/grok_03_billing.png" alt="Billing" width="600"/>
</p>

### 3. Generate an API Key

1. Navigate to the API keys section; there are multiple links to take you there.

    <p align="center">
    <img src="../assets/grok_04_key_links.png" alt="API Key links" width="600"/>
    </p>

1. Click on "Create API Key"

    <p align="center">
    <img src="../assets/grok_05_create_api_key_button.png" alt="Create API Key Button" width="500"/>
    </p>

1. Name your API key (suggested name: GriptapeNodes!)

    <p align="center">
    <img src="../assets/grok_06_create_api_key_page.png" alt="Create API Key Page" width="500"/>
    </p>

1. Click **save**

1. Copy and securely store your API key

    !!! danger "Security Notice"

        It is recommended to save this token in a password locker or secure notes app, so you can find it, but also keep it secure.

        Your access token is a personal identifier for requests made to OpenAI services. Never share it with anyone, and take precautions to avoid displaying it in screenshots, videos, or during screen-sharing sessions.

        Treat it like you would a credit card number.

## Add Your API Key to Griptape Nodes Settings

!!! info "Overview"

    Now that you've set up your xAI account, you need to configure Griptape Nodes to use your API key. This process is straightforward.

### 1. Open the Griptape Nodes Settings Menu

1. Launch Griptape Nodes

1. Look for the **Settings** menu located in the top menu bar (just to the right of File and Edit)

1. Click on **Settings** to open the configuration options

    <p align="center">
    <img src="../assets/gtn_settings_menu.png" alt="Settings Menu" width="500"/>
    </p>

### 2. Add your xAI API Key in API Keys & Secrets

1. In the Configuration Editor, click on **API Keys and Secrets** on the left
1. Scroll down to the **GROK_API_KEY** field
1. Paste the API key you just generated into this field
1. Close the Configuration Editor to automatically save your settings

<p align="center">
    <img src="../assets/grok_07_gtn_settings.png" alt="Grok AI API Key in Settings" width="700"/>
</p>

!!! success "Setup Complete"

    After completing these steps, you should have the ability to use models via your own Grok.ai account credentials.
