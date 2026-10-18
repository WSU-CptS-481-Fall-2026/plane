### What is issue 9396?

This is quite the complex issue that I had as well experienced but others didn't. In short when user attempts to create a project theyre met with a 'Error! Failed to upload cover image' preventing the user from creating the project.
The comments in the issue here claims to have found the fix https://github.com/makeplane/plane/issues/8712. However it didn't work on my system either like many people experienced in issue 9396.

### How I approached solving the issue

I use devtools a lot for this issue. I pressed F12 and click the Network tab. I clicked 'Create Project' and a error appeared under 'uploads'. The important information drived is that the Request URL is http://localhost:8000/uploads with a status code of 404 which means its not found or doesn't exist. To confirm this i searched up 'http://localhost:8000/uploads' and just as I expected this url does not exist at all.

### The Upoad error

![The 404 error](Issue3Error.png)

### Page Not Found

![Page Not Found](PageNotFound.png)

At this point its some form of routing issue. After lots of research docker has caddy on port 80 which then routes the uploads to MiniIO port9000 which is not what the website it reporting.
After research on the caddy website https://caddyserver.com/docs/command-line#caddy-validate has its own vallidation command which didnt work without a docker wrapper in my case. After I got it to run I saw:

### Caddy response

![Caddy error](<caddy error.png>)
So now somthing is wrong with the configuration its requiring the global configuration to go first if any. So I went into the file and moved the global configuration from the bottom to the top of the file.

### Configuration is not correct

![Error](<caddy error-1.png>)

### Where to move the Global Configuration

![Code](CodeMoved.png)

### Configuration is valid

![Passing config](CaddyValid.png)

After this I attempted to create the project again, but the request was still being sent to localhost:8000. So I pressed `CTRL+SHIFT+F` in VS Code and searched for http://localhost:8000, which led me to an .env file. This showed me that the web frontend was reaching the API directly through port 8000. So out of curiosity I removed the 8000 and left http://localhost which defaults to port 80 for HTTP requests. Remember port 80 is also the port caddy is configured on so its listening to for reqeusts to that port.

### The Fix

![Fix](Fix.png)

### It worked and the project created succesffuly.

![It fixed](WORKED.png)

In the end it worked!!! I was able to interact with the project create work items and all functionality worked.

### Testing effort

There wasnt really a way I could test this no code was written besides changing confuiguration. This took me so long and Im surprised I was able to do it.

I did some manual testing:

1. Logged out and back into plane to make sure my change didnt break any other rotues
2. Created and deleted projects.
3. Tried creating porjects with the same name.
4. Tested all functionality within the project like creating tasks, assigning them, chaning the layout, adding labels, etc.
