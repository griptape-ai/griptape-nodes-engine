# How to get and use an OpenAI API Key

While Griptape Nodes provides easy access to OpenAI models by default via your Griptape Cloud account and it's key (this is all handled automatically during install), you might want to connect directly to your own OpenAI account. This gives you full control over your usage, settings, and permissions. If that's what you'd like to do, getting your OpenAI API key is the simple first step.

## Account and API Key Creation

Before you can get API keys for your OpenAI account, you'll need an OpenAI account. To begin, head to [https://openai.com](https://openai.com)

<p align="center">
        <img src="../assets/openai_00_main_page.png" alt="Open AI" width="500"/>
  </p>

!!! info

    If you already have an account, go ahead and skip ahead to [Step 2](#2-create-an-api-key)

### 1. Create an OpenAI account

1. You can either go to the Log In (choose any option, they all create the same account), and then choose "sign up", or go directly to [https://auth.openai.com/create-account](https://auth.openai.com/create-account)

1. Complete the registration process

<p align="center">
      <img src="../assets/openai_01_login_api_platform.png" alt="Login>Platform" width="300"/>
  </p>

### 2. Create an API Key

1. If you're not there already, go back to [https://openai.com/](https://openai.com/) and from the **Log In** button in the top right, select the **API Platform** option

1. Go to the "Dashboard" area - look for it near the top of the page after logging in.

<p align="center">
      <img src="../assets/openai_02_dashboard.png" alt="Dashboard" width="400"/>
  </p>

1. Navigate to the **API Keys** section - look for it on the left after opening the dashboard.

    <p align="center">
    <img src="../assets/openai_03_api_keys.png" alt="API Keys" width="200"/>
    </p>

    !!! warning "Account Verification Required"

        If you encounter issues during key creation, ensure you've **verified** your account which should have been part of the account creation the website guided you through. Complete the verification process before continuing.

    That should present you a screen, that towards the top right looks like this:

    <p align="center">
    <img src="../assets/openai_04_create_new_secret_key_button.png" alt="Create New Secret Key Button" width="500"/>
    </p>

1. Click **Create New Secret Key**, which should take you to a model like this:

    <p align="center">
    <img src="../assets/openai_05_create_new_secret_key_modal.png" alt="Create New Secret Key Modal" width="400"/>
    </p>

1. Set the key permissions to "Read-Only" (feel free to research other options, this is recommended merely for expedience here)

1. Click **Create Secret Key**. That will bring up a window with your new API key. Read and understand the messages there; this really is the only time you'll be able to see or copy this key.

1. Copy and securely store your API key

    !!! danger "Security Notice"

        It is recommended to save this token in a password locker or secure notes app, so you can find it, but also keep it secure.

        Your access token is a personal identifier for requests made to OpenAI services. Never share it with anyone, and take precautions to avoid displaying it in screenshots, videos, or during screen-sharing sessions.

        Treat it like you would a credit card number.

1. Click **Done** to close the key window

## Add Your Secret Key to Griptape Nodes Settings

!!! info "Overview"

    Now that you've set up your OpenAI account, you need to configure Griptape Nodes to use your secret key. This process is straightforward.

### 1. Open the Griptape Nodes Settings Menu

1. Launch Griptape Nodes

1. Look for the **Settings** menu located in the top menu bar (just to the right of File and Edit)

1. Click on **Settings** to open the configuration options

    <p align="center">
    <img src="../assets/gtn_settings_menu.png" alt="Settings Menu" width="500"/>
    </p>

### 2. Add your OpenAI Secret Key in API Keys & Secrets

1. In the Configuration Editor, click on **API Keys and Secrets** on the left
1. Scroll down to the **OPENAI_API_KEY** field
1. Paste the secret key you just generated into this field
1. Close the Configuration Editor to automatically save your settings

<p align="center">
    <img src="../assets/openai_06_gtn_settings.png" alt="Open AI Secret Key in Settings" width="600"/>
  </p>

!!! success "Setup Complete"

    After completing these steps, you should have the ability to use models via your own account credentials.
