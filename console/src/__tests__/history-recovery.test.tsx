import { render, screen, act } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { it, expect, vi } from 'vitest';
import { PrototypeApp } from '../prototype/PrototypeApp';
import { ApiProvider } from '../state/api';
import { HttpSessionApi } from '../api/http';
import { SESSIONS } from '../api/fixtures';

it('can recover an initial detail failure when a cached transcript is visible', async()=>{
 const session=structuredClone(SESSIONS[0]);
 const client=new HttpSessionApi();
 vi.spyOn(client,'listSessions').mockResolvedValue([]);
 vi.spyOn(client,'listProviders').mockResolvedValue([]);
 vi.spyOn(client,'listModels').mockResolvedValue([]);
 vi.spyOn(client,'readSessionCache').mockResolvedValue(session);
 vi.spyOn(client,'writeSessionCache').mockResolvedValue(undefined);
 vi.spyOn(client,'getSession').mockRejectedValueOnce(new Error('Temporary detail outage')).mockResolvedValue(session);
 const sub=vi.spyOn(client,'subscribe').mockImplementation((_id,handlers)=>{handlers.onOpen?.();return ()=>{};});
 const view=render(<ApiProvider client={client}><MemoryRouter initialEntries={['/sessions/'+session.id]}><PrototypeApp/></MemoryRouter></ApiProvider>);
 await screen.findByText('Temporary detail outage');
 expect(screen.getByRole('heading',{level:1,name:session.title})).toBeVisible();
 fireEvent.click(await screen.findByRole('button',{name:'Retry connection'}));
 await waitFor(()=>expect(sub).toHaveBeenCalledTimes(1));
 expect(detail).toHaveBeenCalledTimes(2);
 view.unmount();
});
