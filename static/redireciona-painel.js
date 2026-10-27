// Links antigos do painel (/#/dashboard, /#/alertas...) agora ficam em /painel
if (location.hash.startsWith('#/')) location.replace('/painel' + location.hash);
