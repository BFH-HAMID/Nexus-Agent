// Extract the libraries' static CSS rather than weakening the HUD's CSP for style tags.
import {build} from 'esbuild';
import fs from 'node:fs/promises';
const css=new Set();
await build({entryPoints:['world.js'],bundle:true,minify:true,format:'esm',outfile:'../../nexus/hud/world/assets/world.js',legalComments:'linked',plugins:[{name:'local-static-library-css',setup(b){b.onLoad({filter:/node_modules\/(globe\.gl|three-render-objects|float-tooltip)\/dist\/.*\.mjs$/},async args=>{let code=await fs.readFile(args.path,'utf8');const m=code.match(/var css_248z = ("(?:\\.|[^"\\])*");/);if(m){css.add(JSON.parse(m[1]));code=code.replace(/styleInject\(css_248z\);/g,'/* Static CSS shipped as world.vendor.css. */');}return {contents:code,loader:'js'};});}}]});
await fs.writeFile('../../nexus/hud/world/assets/world.vendor.css',[...css].join('\n'));
// Full license notices remain available locally, beyond the minifier's brief notices.
const names=[];for(const n of await fs.readdir('node_modules')){if(n.startsWith('.'))continue;if(n.startsWith('@')){for(const child of await fs.readdir('node_modules/'+n))names.push(n+'/'+child);}else names.push(n);}names.sort();
let notices='Third-party libraries used by the locally bundled World widget.\n\n';
for(const name of names){let p;try{p=JSON.parse(await fs.readFile(`node_modules/${name}/package.json`,'utf8'));}catch{continue;}notices+=`${name} ${p.version} / ${p.license}\n`;for(const file of ['LICENSE','LICENSE.md','LICENSE.txt']){try{notices+=await fs.readFile(`node_modules/${name}/${file}`,'utf8')+'\n\n';break;}catch{}}}
await fs.writeFile('../../nexus/hud/world/assets/THIRD-PARTY.txt',notices);
