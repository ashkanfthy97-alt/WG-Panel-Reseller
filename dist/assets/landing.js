(() => {
  const toggle = document.querySelector('#languageToggle');
  const saved = localStorage.getItem('wirepanel-language');
  let language = saved === 'en' ? 'en' : 'fa';
  function render() {
    document.documentElement.lang = language;
    document.documentElement.dir = language === 'fa' ? 'rtl' : 'ltr';
    toggle.textContent = language === 'fa' ? 'English' : 'فارسی';
    for (const element of document.querySelectorAll('[data-fa][data-en]')) {
      element.innerHTML = element.dataset[language];
    }
  }
  toggle.addEventListener('click', () => {
    language = language === 'fa' ? 'en' : 'fa';
    localStorage.setItem('wirepanel-language', language);
    render();
  });
  render();
})();
