# Setup for Nodes that use Hugging Face

## Account and Token Creation

This guide will walk you through setting up a Hugging Face account, creating an access token, and installing the required models for use with Griptape Nodes.

!!! info

    If you already have an account skip ahead to [Step 2](#2-create-an-access-token)

### 1. Create a new account on Hugging Face

1. Go to [https://huggingface.co/](https://huggingface.co/)
1. Click **Sign Up** in the top-right corner
1. Complete the verification step to prove you're not a robot

<p align="center">
    <img src="../assets/huggingface_00_MainPage.png" alt="HF Site" width="500"/>
  </p>

<p align="center">
    <img src="../assets/huggingface_01_signup.png" alt="Signup" width="300"/>
  </p>

### 2. Create an Access Token

1. Access Your Account Settings

1. Log in to your Hugging Face account

1. Click on your profile icon in the top right corner

1. Select **Settings** from the dropdown menu (or go directly to [Settings](https://huggingface.co/settings/profile/))

    <p align="center">
    <img src="../assets/huggingface_02_Settings.png" alt="Settings" width="500"/>
    </p>

!!! warning "Email Verification Required"

    If you encounter issues during token creation, ensure you've verified your email address. Complete the verification process before continuing.

1. Navigate to **Access Tokens** in the settings menu

<p align="center">
  <img src="../assets/huggingface_03_AccessTokens.png" alt="Access Tokens" width="500"/>
</p>

1. Click **Create new token** in the top right area

1. Select **Read** as the token type for the access you'll need

<p align="center">
  <img src="../assets/huggingface_04_TokenRead.png" alt="Token Read" width="500"/>
</p>

1. Give your token a descriptive name (GriptapeNodes, for example)
1. Click **Create Token**. That will bring up a window with you new token in it. Read and understand the messages there; this really is the only time you'll be able to see or copy this key.

<p align="center">
  <img src="../assets/huggingface_05_SaveToken.png" alt="Save Token" width="400"/>
</p>

1. Copy and securely store your token
1. Click **Done** to close the token window.

!!! danger "Security Notice"

    It is recommended to save this token in a password locker or secure notes app, so you can find it, but also keep it secure.

    Your access token is a personal identifier for requests made to Hugging Face services. Never share it with anyone, and take precautions to avoid displaying it in screenshots, videos, or during screen-sharing sessions.

    Treat it like you would a credit card number.

## Install Required Files

Now that you have a token associated with your account, you can install the Hugging Face CLI (Command Line Interface) to interact with Hugging Face from the command line.

### 1. Install the Hugging Face CLI

Open a terminal and run:

```bash
pip install -U "huggingface_hub[cli]"
```

For more information, visit the [official CLI documentation](https://huggingface.co/docs/huggingface_hub/main/en/guides/cli).

### 2. Login with Your Token

In your terminal, authenticate with the access token you made earlier:

```bash
huggingface-cli login
```

You'll be prompted to enter your token.

### 3. Install Models as Required

Each Hugging Face node is designed to work with specific models. While some nodes work with only one model, others are compatible with multiple options. The table below outlines which models can be used with each node:

| Node              | Compatible Model(s)        |
| ----------------- | -------------------------- |
| SPAN Upscale      | 4x-ClearRealityV1.pth      |
| Flux              | FLUX.1-dev, FLUX.1-schnell |
| Flux Post Upscale | FLUX.1-dev, FLUX.1-schnell |

## Model Installation Considerations

You have two options for model installation. Your choice depends on your specific workflow requirements, available disk space, and internet connection speed.

1. **Selective Installation**: Install only the specific models needed for the nodes you plan to use.

    - **Advantages**: Reduced download time and disk space usage.
    - **Disadvantages**: Limited functionality until additional models are installed.

1. **Complete Installation**: Download all available models.

    - **Advantages**: Full access to all node capabilities without additional downloads.
    - **Disadvantages**: Longer initial download time and greater disk space requirements.

!!! note "Download Time"

    These model downloads are quite large an may collectively take anywhere from 30 minutes to several hours to complete, depending on your internet connection speed. You can continue to the last step while downloads are ongoing.

!!! info "Download Location"

    Models will be downloaded into your Hugging Face Hub Cache Directory. To see where this is, you can use the huggingface-cli, and look for HF_HUB_CACHE.

    Running this command in the terminal will produce a list with several entries

    ```
    huggingface-cli env
    ```

    Look for an entry that starts with `- HF_HUB_CACHE:`

    ```
    - HF_HUB_CACHE: /Users/jason/.cache/huggingface/hub
    ```

#### For SPAN Upscale

```bash
huggingface-cli download skbhadra/ClearRealityV1 4x-ClearRealityV1.pth
```

#### For Flux and Flux Post Upscale

```bash
huggingface-cli download black-forest-labs/FLUX.1-schnell
```

#### For FLUX.1-dev

```bash
huggingface-cli download black-forest-labs/FLUX.1-dev
```

!!! warning "Download errors"

    It is possible to encounter errors during download that start like this:

    ```
    Cannot access gated repo for url https://huggingface.co...
    ```

    The end of those errors will contain a link, though, with instructions to request access.

    Follow those instructions:

    Visit [https://huggingface.co/black-forest-labs/FLUX.1-schnell](https://huggingface.co/black-forest-labs/FLUX.1-schnell) to ask for access. When that is successful you should see a message about being granted access, and you can try to download again.

    <p align="center">
      <img src="../assets/huggingface_06_gated_model.png" alt="Gated model" width="350"/>
    </p>

## Add Your Token to Griptape Nodes settings

!!! info "Overview"

    Now that you've set up your Hugging Face account and installed the required models, you need to configure Griptape Nodes to use your token. This process is straightforward.

### 1. Open the Griptape Nodes Settings Menu

1. Launch Griptape Nodes
1. Look for the **Settings** menu located in the top menu bar (just to the right of File and Edit)
1. Click on **Settings** to open the configuration options

<p align="center">
  <img src="../assets/huggingface_07_GN_Settings.png" alt="Settings Menu" width="500"/>
</p>

### 2. Add your Hugging Face Token in API Keys & Secrets

1. In the Configuration Editor, locate **API Keys and Secrets** in the bottom left
1. Click to expand this section
1. Scroll down to the **HUGGINGFACE_HUB_ACCESS_TOKEN** field
1. Paste your previously created token into this field
1. Close the Configuration Editor to automatically save your settings

<p align="center">
  <img src="../assets/huggingface_08_GN_HFToken.png" alt="Token Configuration" width="500"/>
</p>

!!! success "Setup Complete"

    After completing these steps, the Hugging Face Nodes should be ready to use in Griptape Nodes!
