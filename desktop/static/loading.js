window.llmsDesktopShowError = message => {
    document.querySelector('#status').hidden = true
    document.querySelector('#error').hidden = false
    document.querySelector('#error-message').textContent = message
}
