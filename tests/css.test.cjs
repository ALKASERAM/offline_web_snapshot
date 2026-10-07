const {test}=require('node:test');
const assert=require('node:assert/strict');
const {rewriteCSS}=require('../src/offline_snapshot/css_urls.js');
function rewrite(source){const seen=[];return {text:rewriteCSS(source,url=>{seen.push(url);return 'blob:saved/'+seen.length;}),seen};}

test('CSS URLs honor comments, quoted parentheses and Unicode escapes',()=>{
  const result=rewrite(String.raw`/* url(no.png) */ .x{content:"url(fake.png)";background:URL("a)b.png"),u\72l(a\20 b\(c\).svg)}`);
  assert.deepEqual(result.seen,['a)b.png','a b(c).svg']);
  assert.match(result.text,/content:"url\(fake.png\)"/);
  assert.match(result.text,/background:url\("blob:saved\/1"\),url\("blob:saved\/2"\)/);
});
test('CSS import strings and image-set candidates are URLs, type strings are not',()=>{
  const result=rewrite(`@import /* note */ 'theme.css' layer(base) screen; .x{background:image-set('small.png' 1x type('image/png'), url(big.png) 2x),linear-gradient(red,blue)}`);
  assert.deepEqual(result.seen,['theme.css','small.png','big.png']);
  assert.match(result.text,/type\('image\/png'\)/);
});
test('CSS token scanner leaves invalid URLs, unrelated strings and non-CSS values alone',()=>{
  const source=`.x{content:'image-set("literal.png")';background:url(bad space.png);color:red}`;
  const result=rewrite(source);assert.deepEqual(result.seen,[]);assert.equal(result.text,source);
  assert.equal(rewriteCSS(null,()=>assert.fail()),null);assert.equal(rewriteCSS('red',()=>assert.fail()),'red');
});
test('CSS rewrite preserves unchanged URLs and safely quotes replacement values',()=>{
  const source=String.raw`.x{filter:url(#mask);background:url("data:image/svg+xml,a)b")}`;
  assert.equal(rewriteCSS(source,url=>url),source);
  assert.equal(rewriteCSS('url(a)',()=> 'x"y\\z\n'),'url("x\\"y\\\\z\\a ")');
});
