import { useState, useEffect } from 'react';
import axios from 'axios';
import { MapContainer, ImageOverlay, Marker, Popup } from 'react-leaflet';
import 'leaflet/dist/leaflet.css';
import L from 'leaflet';

// Correção do ícone do marcador
import icon from 'leaflet/dist/images/marker-icon.png';
import iconShadow from 'leaflet/dist/images/marker-shadow.png';

let DefaultIcon = L.icon({
    iconUrl: icon,
    shadowUrl: iconShadow,
    iconSize: [25, 41],
    iconAnchor: [12, 41]
});
L.Marker.prototype.options.icon = DefaultIcon;


function Mapa() {
  const [quartos, setQuartos] = useState([]);
  const [loading, setLoading] = useState(true);

  const imageWidth = 200;
  const imageHeight = 800;
  const API_URL = 'http://localhost:8000/api/v1/planta/dados';
  const imageUrl = '/planta.svg';

  useEffect(() => {
    const fetchData = async () => {
      try {
        const response = await axios.get(API_URL);
        setQuartos(response.data);
      } catch (error) {
        console.error("Erro ao buscar dados da planta:", error);
      } finally {
        setLoading(false);
      }
    };
    fetchData();
  }, []);

  if (loading) {
    return <div style={{ textAlign: 'center', padding: '50px' }}>Carregando dados do mapa...</div>;
  }

  const bounds = L.latLngBounds([0, 0], [imageHeight, imageWidth]);

  return (
    // O container do mapa não precisa de um div extra com padding.
    // Deixe-o ocupar o espaço que o componente pai lhe der.
    <MapContainer
      crs={L.CRS.Simple}
      bounds={bounds}
      style={{ height: 'calc(100vh - 80px)', width: '100%', backgroundColor: '#e9e9e9' }}
      
      // --- MELHORIAS ADICIONADAS AQUI ---
      maxBounds={bounds} // 1. Impede que o usuário arraste o mapa para fora dos limites da imagem.
      maxBoundsViscosity={1.0} // 2. Deixa a "parede" do limite 100% sólida. (Use 0.5 para um efeito "elástico").
      // --- FIM DAS MELHORIAS ---
      >
      
      <ImageOverlay url={imageUrl} bounds={bounds} />

      {quartos.map(quarto => {
        if (quarto.pos_x === null || quarto.pos_y === null) return null;

        const position = [imageHeight - quarto.pos_y, quarto.pos_x];

        return (
          <Marker key={quarto.id} position={position}>
            <Popup>
              <b>{quarto.nome}</b><br />
              Ativos: {quarto.assets_count}
            </Popup>
          </Marker>
        );
      })}
    </MapContainer>
  );
}

function App() {
  // --- MUDANÇA AQUI ---
  // Removemos o padding e estilizamos para que o layout ocupe a tela toda.
  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100vh' }}>
      <h1 style={{ padding: '0 20px', flexShrink: 0 }}>Planta Baixa Interativa (React)</h1>
      <main style={{ flexGrow: 1, padding: '0 20px 20px 20px' }}>
        <Mapa />
      </main>
    </div>
  );
}

export default App;