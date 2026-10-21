"""Native readPage evidence must reconstruct complete source bytes."""
import copy
import importlib.util
import json
import pathlib
import shutil
import subprocess
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
HELPER = ROOT / 'components/skills/maka_read_aggregation.py'


class NativeReadAggregationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        completed = subprocess.run(['mise', '-C', str(ROOT / 'components/maka'), 'which', 'maka'], text=True, capture_output=True, check=True)
        package = pathlib.Path(completed.stdout.strip()).parent.parent / 'maka-agent'
        script = r"""
import {readPage} from PAGE;
const cases = ['alpha\r\nbeta😀\n' + 'råd😀'.repeat(100) + '\nlast\n', 'one\n'+'x'.repeat(2000), '', 'single😀', 'a'.repeat(10000)+'\n'+'b'.repeat(10000)];
const rows=cases.map((text,index)=>{
 const path=index===4?'/native/two-long-lines.md':'/native/reference-'+index+'.md';let input={path};const result=[];
 for(let i=0;i<100;i++){
  const value=index===4?readPage(text,input):readPage(text,input,420);const id='read-'+index+'-'+i;
  result.push({call:{id,name:'Read',args:input},response:{id,name:'Read',result:{value}}});
  if(value.next===null)return {text,path,rows:result};
  input=value.next;
 }
 throw Error('native page bound exceeded');
});
console.log(JSON.stringify(rows));
""".replace('PAGE', json.dumps((package / 'node_modules/@maka/runtime/dist/read-page.js').as_uri()))
        result = subprocess.run([shutil.which('node'), '--input-type=module', '-e', script], capture_output=True, text=True, check=True)
        cls.cases = json.loads(result.stdout)

    def helper(self):
        self.assertTrue(HELPER.is_file(), 'portable native Read aggregation helper is missing')
        spec = importlib.util.spec_from_file_location('read_aggregation', HELPER)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def prove(self, case):
        return self.helper().complete_reference_chains(case['rows'], {'reference': (pathlib.Path(case['path']), case['text'].encode())}, pathlib.Path('/native'))

    def test_installed_native_pages_reconstruct_utf16_partial_lf_crlf_and_empty_bytes(self):
        for case in self.cases:
            with self.subTest(text=case['text'][:25]):
                proof = self.prove(case)[0]
                self.assertEqual(proof['reconstructed_bytes'], case['text'].encode())
                self.assertEqual(len(proof['pages']), len(case['rows']))

    def test_native_nonterminal_page_starting_midline_and_ending_at_lf_boundary(self):
        case = self.cases[4]
        pages = [row['response']['result']['value'] for row in case['rows']]
        self.assertEqual(len(pages), 4)
        self.assertEqual([len(page['content']) for page in pages], [7313, 2687, 7312, 2688])
        self.assertEqual([page['returnedLines'] for page in pages], [0, 1, 0, 1])
        self.assertTrue(all(page.get('partialLine') is True for page in pages))
        self.assertIsNotNone(pages[1]['next'])
        self.assertIn('?at=10001&', pages[1]['next']['path'])
        proof = self.prove(case)[0]
        self.assertEqual(proof['sha256'], '77a50569d618afc1ee84d010fb8cebd27aa70229cdd185d0b9aaf95551803edf')
        self.assertEqual(proof['reconstructed_bytes'], case['text'].encode())
        self.assertEqual(len(proof['pages']), 4)

    def test_midline_boundary_page_rejects_wrong_line_count_or_utf16_cursor(self):
        for damage in ('wrong_lines', 'missing_lf', 'gap', 'skip_non_lf', 'wrong_source'):
            with self.subTest(damage=damage):
                case = copy.deepcopy(self.cases[4])
                rows = case['rows']
                page = rows[1]['response']['result']['value']
                if damage == 'wrong_lines':
                    page['returnedLines'] = 0
                elif damage == 'wrong_source':
                    page['content'] = 'b' + page['content'][1:]
                else:
                    page_index = 0 if damage == 'skip_non_lf' else 1
                    value = rows[page_index]['response']['result']['value']
                    old, new = {'missing_lf': ('10001', '10000'),
                                'gap': ('10001', '10002'),
                                'skip_non_lf': ('7313', '7314')}[damage]
                    value['next']['path'] = value['next']['path'].replace('?at=' + old, '?at=' + new)
                    rows[page_index + 1]['call']['args'] = copy.deepcopy(value['next'])
                with self.assertRaises(RuntimeError):
                    self.prove(case)

    def test_incomplete_or_misattributed_pages_fail_closed(self):
        for damage in ('missing', 'duplicate', 'out_of_order', 'wrong_uri', 'wrong_hash', 'gap', 'truncated', 'wrong_total', 'result_join', 'noncanonical_position', 'terminal_cursor', 'wrong_path'):
            with self.subTest(damage=damage):
                case=copy.deepcopy(self.cases[0]);rows=case['rows'];first=rows[0]['response']['result']['value'];last=rows[-1]['response']['result']['value']
                if damage=='missing': rows.pop()
                elif damage=='duplicate': rows.append(copy.deepcopy(rows[1]))
                elif damage=='out_of_order': rows.reverse()
                elif damage in ('wrong_uri','wrong_hash','gap','noncanonical_position'):
                    uri=first['next']['path']
                    if damage=='wrong_uri': changed=uri.replace('maka://read/','maka://wrong/')
                    elif damage=='wrong_hash': changed=uri.split('&sha=')[0]+'&sha='+'0'*32
                    elif damage=='noncanonical_position': changed=uri.replace('?at=','?at=+')
                    else:
                        from urllib.parse import urlsplit,parse_qs
                        value=parse_qs(urlsplit(uri).query)['at'][0];changed=uri.replace('at='+value,'at='+str(int(value)+1))
                    first['next']['path']=changed;rows[1]['call']['args']['path']=changed
                elif damage=='truncated': last['content']=last['content'][:-1]
                elif damage=='wrong_total': last['totalLines']+=1
                elif damage=='result_join': rows[1]['response']['id']='unrelated-call'
                elif damage=='terminal_cursor': last.pop('next')
                else: rows[0]['call']['args']['path']='/elsewhere/reference.md'
                with self.assertRaises(RuntimeError): self.prove(case)


if __name__ == '__main__':
    unittest.main()
