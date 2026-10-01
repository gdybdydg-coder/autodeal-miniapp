"""Build the in-chat RAM preview from the same authored UI, without transport."""
import argparse
import re
from pathlib import Path

ROOT = Path(__file__).parent


def build():
    page = (ROOT / 'trial.html').read_text()
    start = page.index('<div id="autodeal-payment-trial"')
    end = page.index('<script src="/fixture-config.js">')
    def cursor(match):
        attrs = match[1]
        if 'class="' in attrs:
            attrs = re.sub(r'class="([^"]*)"',
                           lambda found: 'class="' + found[1] + ' cursor-interaction"', attrs)
        else:
            attrs = ' class="cursor-interaction"' + attrs
        return '<button' + attrs + '>'
    markup = re.sub(r'<button([^>]*)>', cursor, page[start:end])
    css = (ROOT / 'payment-ui.css').read_text()
    css += '\n#autodeal-payment-trial header{padding-right:100px}\n'
    scripts = [(ROOT / name).read_text() for name in
               ('workflow.js', 'demo-adapter.js', 'payment-ui.js')]
    bootstrap = '''
(function(){
  const root=document.getElementById('autodeal-payment-trial');
  window.AutoDealTrialApi=window.AutoDealTrialMemory.create();
  window.AutoDealTrialApp=window.AutoDealPaymentUI.mount(root,window.AutoDealTrialApi);
})();
'''
    return markup + '<style>\n' + css + '</style>\n' + ''.join(
        '<script>\n' + script + '\n</script>\n' for script in scripts + [bootstrap])


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('destination', nargs='?', type=Path,
                        default=ROOT / 'payment-trial-inline.html')
    args = parser.parse_args()
    args.destination.write_text(build())
